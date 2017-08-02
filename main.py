from retryable_request import DeskAttachmentRequest, DeskCustomerRequest, \
    DeskMessageRequest, DeskTicketRequest, CheckUpload, ZendeskUpload, \
    ZendeskUserPostRequest, ZendeskTicketPostRequest, ZendeskTicketIDRequest, \
    ZendeskUpdateRequest, ZendeskTicketCommentCount, ZendeskVerification, ZendeskUserRequest, \
    ZendeskSearch, handle_retries

import argparse
import collections
import json
import logging
import math

from Queue import Queue
from collections import defaultdict
from collections import namedtuple
from constants import AGENT_ID, PROCESSES
from multiprocessing.pool import ThreadPool
from zendesk_desk_models import ZMessageCreate, ZMessageUpdate, ZTicket, ZTicketUpdate, ZUser

TICKET_STATUSES = ['open', 'closed']
ROLES = ['end-user', 'agent', 'admin']
AttachmentTuple = namedtuple('AttachmentTuple', ['token', 'message_uri'])
post_queue = Queue()
update_queue = Queue()

logger = logging.getLogger("migrate_to_zendesk")
FORMAT = '[%(asctime)s] %(levelname)s %(thread)d %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)
POOL = ThreadPool(processes=PROCESSES)
global_results = collections.deque()


def migrate_user(desk_user):
    logger.info("Creating user: %s" % desk_user.id)
    zd_user = ZUser()
    zd_user.desk_user_to_ZUser(user=desk_user)
    post_queue.put(zd_user)

    global_results.appendleft(POOL.apply_async(post_users_zendesk))


def post_users_zendesk(batch_size=100):
    zendesk_users = []
    if post_queue.qsize() < batch_size:
        return
    logger.info("Posting %d users..." % batch_size)
    zendesk_users.extend((post_queue.get().to_primitive()) for i in xrange(batch_size))
    data = json.dumps({"users": zendesk_users})
    handle_retries(retryable_request=ZendeskUserPostRequest, get_request_kwargs={'data': data})


def migrate_ticket(ticket, agent):  # noqa
    desk_ticket = ticket_json_to_desk_obj(ticket)
    if not desk_ticket:
        return
    attachment_tuples = []
    for attachment in desk_ticket.attachments:
        content = handle_retries(retryable_request=CheckUpload, get_request_kwargs={'url': attachment.url})
        if content:
            uploaded_attachment = handle_retries(retryable_request=ZendeskUpload, get_request_kwargs={'params': {'filename': attachment.file_name}, 'data': content})
            attachment_tuples.append(AttachmentTuple(token=uploaded_attachment.get('upload', {}).get('token', ''), message_uri=attachment.message_uri))
    zd_ticket = desk_ticket_to_ZTicket(ticket=desk_ticket, agent_id=agent, attachment_tuples=attachment_tuples)
    if not zd_ticket:
        return
    logger.info("Creating OR updating ticket: %d" % desk_ticket.id)
    id = handle_retries(retryable_request=ZendeskTicketIDRequest, get_request_kwargs={'url': "/api/v2/search.json",
                                                                                      'params': {'query': 'type:ticket external_id:%d' % desk_ticket.id}})
    # Ticket already exists
    if id > 0:
        zd_ticket.id = id
        num_comments = handle_retries(retryable_request=ZendeskTicketCommentCount, get_request_kwargs={'url': "/api/v2/tickets/%d.json" % (id),
                                                                                                       'params': {'include': 'comment_count'}})
        comments_to_add = len(zd_ticket.comments) - num_comments
        logger.info("Adding %d comments to ticket %d already in zendesk" % (comments_to_add, id))
        if comments_to_add > 0:
            individual_tickets = create_ZTickets_for_comments(zd_ticket, comments_to_add)
            [update_queue.put(i_ticket) for i_ticket in individual_tickets]
        zd_ticket.comments = None
        update_queue.put(zd_ticket)  # In all cases (new comment/no new comment), we should add the original ticket to update status/etc.
        global_results.appendleft(POOL.apply_async(update_tickets_zendesk))
    elif id == 0:
        post_queue.put(zd_ticket)
        global_results.appendleft(POOL.apply_async(post_tickets_zendesk))
    else:
        logger.error("Could not add ticket %d to the queue - checking existence failed" % desk_ticket.id)


def ticket_json_to_desk_obj(ticket):
    """Create ticket schmantics objects for desk data."""
    ticket.notes = []
    ticket.attachments = []
    if ticket.num_replies != 0:
        upper_limit = int(math.ceil(ticket.num_replies / 100.) + 1)
        for i in xrange(1, upper_limit):
            ticket.messages.extend(handle_retries(retryable_request=DeskMessageRequest,
                                                  get_request_kwargs={"params": {'page': i, 'per_page': 100},
                                                                      "url": "/api/v2/cases/%d/replies" % (ticket.id)}))
    if ticket.num_notes != 0:
        upper_limit = int(math.ceil(ticket.num_notes / 100.) + 1)
        for i in xrange(1, upper_limit):
            ticket.notes.extend(handle_retries(retryable_request=DeskMessageRequest,
                                get_request_kwargs={"params": {'page': i, 'per_page': 100},
                                                    "url": "/api/v2/cases/%d/notes" % (ticket.id)}))
    if ticket.num_attachments != 0:
        upper_limit = int(math.ceil(ticket.num_attachments / 100.) + 1)
        for i in xrange(1, upper_limit):
            ticket.attachments.extend(handle_retries(retryable_request=DeskAttachmentRequest,
                                      get_request_kwargs={"params": {'page': i, 'per_page': 100},
                                                          "url": "/api/v2/cases/%d/attachments" % (ticket.id)}))
    return ticket

def desk_ticket_to_ZTicket(ticket, agent_id, attachment_tuples):  # noqa
    """Convert Desk Ticket object to Zendesk ZTicket object."""
    zmessages = []
    creator_id = handle_retries(retryable_request=ZendeskSearch, get_request_kwargs={"url": "/api/v2/search.json",
                                                                                     "params": {"query": "type:user %d" % ticket.user_id}})
    if creator_id == 0 or not creator_id:  # Must migrate users BEFORE migrating tickets
        logger.error("Could not get creator_id for desk ticket %d...not posting or adding" % ticket.id)
        return
    remaining_attachments = attachment_tuples
    for message in ticket.messages:
        if not message.body.strip():  # Zendesk requires all comments to have a body, but Desk does not have this requirement
            message.body = "No message"
        # we set created_at to updated_at because ZD has no draft message status, but Desk does.
        zmessage = ZMessageCreate({'value': message.body, 'created_at': message.updated_at, 'author_id': agent_id, 'uploads': [], 'public': True}, strict=False, partial=False)
        zmessage.uploads = [at.token for at in attachment_tuples if at.message_uri == message.uri]
        remaining_attachments = [at for at in remaining_attachments if at.message_uri != message.uri]  # Remove attachment we just added
        zmessage.author_id = agent_id
        if message.direction == 'in':
            zmessage.author_id = creator_id
            if message.creator_id != ticket.user_id:
                zd_user_id = handle_retries(retryable_request=ZendeskSearch, get_request_kwargs={"url": "/api/v2/search.json",
                                            "params": {"query": "type:user %d" % message.creator_id}})
                if zd_user_id:
                    zmessage.author_id = zd_user_id
                else:
                    logger.error("Could not get creator_id for desk message %d...not posting or adding" % ticket.id)
                    return
        zmessages.append(zmessage)
    for note in ticket.notes:
        if not note.body.strip():
            note.body = "No message"
        zmessage = ZMessageCreate({'value': note.body, 'created_at': note.updated_at, 'author_id': agent_id, 'uploads': [], 'public': False}, strict=False, partial=False)
        zmessages.append(zmessage)

    zticket = ZTicket({'comments': zmessages, 'subject': ticket.subject, 'priority': 'low', 'status': ticket.status, 'external_id': ticket.id,
                       'requester_id': creator_id, 'assignee_id': agent_id, 'tags': ['from_desk'],
                       'created_at': ticket.created_at, 'solved_at': ticket.resolved_at, 'updated_at': ticket.updated_at}, strict=False, partial=False)
    # Leftover attachments with no associated reply get added onto first message
    zticket.comments[0].uploads = [at.token for at in remaining_attachments]
    if 4 <= ticket.priority <= 6:
        zticket.priority = 'normal'
    elif 7 <= ticket.priority <= 9:
        zticket.priority = 'high'
    elif ticket.priority == 10:
        zticket.priority = 'urgent'

    if ticket.status == 'resolved':
        zticket.status = 'solved'
    return zticket


def create_ZTickets_for_comments(zd_ticket, num_new):
    """Zendesk only updates one comment per API call, so create an object per ticket to update each comment"""
    zdtickets = []
    for message in zd_ticket.comments:
        zticket_update = ZTicketUpdate({'id': zd_ticket.id}, strict=False, partial=False)
        zticket_update.comment = ZMessageUpdate({'body': message.value, 'created_at': message.updated_at, 'author_id': message.author_id, 'uploads': message.uploads, 'public': message.public}, strict=False, partial=False)
        zdtickets.append(zticket_update)
    # Only add the newest comments, specified by the difference between number of comments in ZD and comments in Desk
    zdtickets.sort(key=lambda x: x.comment.created_at, reverse=True)
    return zdtickets[:num_new]


def post_tickets_zendesk(batch_size=100):
    zendesk_tickets = []
    if post_queue.qsize() < batch_size:
        return
    logger.info("Posting %d tickets..." % batch_size)
    zendesk_tickets.extend((post_queue.get().to_primitive()) for i in xrange(batch_size))
    data = json.dumps({"tickets": zendesk_tickets})
    handle_retries(retryable_request=ZendeskTicketPostRequest, get_request_kwargs={'data': data})


def update_tickets_zendesk(batch_size=100):
    zendesk_tickets = []
    if update_queue.qsize() < batch_size:
        return
    zendesk_tickets.extend(update_queue.get() for i in xrange(batch_size))
    dedup_dict = defaultdict(list)  # One API call can't update the same ticket twice
    for ticket in zendesk_tickets:
        dedup_dict[ticket.id].append(ticket)
    ztickets_deduped = []
    for id, item in dedup_dict.iteritems():
        ztickets_deduped.append(item[0].to_primitive())
        if len(item) > 1:
            logger.info("There were %d dupes" % (len(item) - 1))
            for dupe in item[1:]:
                update_queue.put(dupe)
    logger.info("Updating ticket...")
    data = json.dumps({"tickets": ztickets_deduped})
    logger.info(data)
    handle_retries(retryable_request=ZendeskUpdateRequest, get_request_kwargs={'data': data})


def pool_controller(retryable_request, get_request_kwargs, agent_id):  # noqa
    # Get first page and total number of apges
    num_pages = handle_retries(retryable_request=retryable_request, get_pages=True, get_request_kwargs=get_request_kwargs)
    if not num_pages:
        logger.error("Error: Could not get number of pages")
        return
    logger.info("Number of pages %d" % num_pages)
    for i in xrange(1, num_pages + 1):
        logger.info("Processing page %d" % i)
        # Returns list of dictionaries for ticket objects OR list of user objects
        object_list = []
        migrating_users = retryable_request == DeskCustomerRequest
        if migrating_users:
            kwargs = {'params': {'embed': 'facebook_user,twitter_user', 'page': i, 'per_page': 100}}
        else:
            kwargs = {'params': {'embed': 'customer, message', 'page': i, 'per_page': 100}}
        object_list = POOL.apply(handle_retries,
                                 kwds={"retryable_request": retryable_request,
                                       "get_request_kwargs": kwargs})
        for elem in object_list:
            if migrating_users:
                global_results.appendleft(POOL.apply_async(migrate_user, kwds={"desk_user": elem}))
            else:
                global_results.appendleft(POOL.apply_async(migrate_ticket, kwds={"ticket": elem, "agent": agent_id}))
    while len(global_results) > 0:
        result = global_results.pop()
        result.get()
    POOL.close()
    POOL.join()
    if migrating_users:
        post_func = post_users_zendesk
    else:
        post_func = post_tickets_zendesk

    flush_queues(post_func)
    return num_pages


def flush_queues(post_func):
    # Update queue flushes individually because of API restrictions
    if not update_queue.empty():
        logger.info("Cleaning up updating queue")
        while update_queue.qsize() >= 100:
            update_tickets_zendesk(batch_size=100)
        while not update_queue.empty():
            update_tickets_zendesk(batch_size=update_queue.qsize())

    # Post queue batch flushes
    if not post_queue.empty():
        logger.info("Cleaning up posting queue")
        while post_queue.qsize() >= 100:
            post_func()
        post_func(batch_size=post_queue.qsize())


def get_global_results():
    return global_results


def main():
    parser = argparse.ArgumentParser(description="Migrate support tickets from desk to zendesk.")
    parser.add_argument("--mode", help="Specify either (u)sers or (t)ickets to migrate")
    options = parser.parse_args()
    mode = options.mode
    # hardcode the agent from which all tickets are being posted
    agent_id = handle_retries(retryable_request=ZendeskUserRequest, get_request_kwargs={"url": "/api/v2/users/%s" % AGENT_ID})
    if mode == 'u':
        pool_controller(retryable_request=DeskCustomerRequest, agent_id=agent_id, get_request_kwargs={'params': {'embed': 'facebook_user,twitter_user', 'page': 1, 'per_page': 100}})
    elif mode == 't':
        pool_controller(retryable_request=DeskTicketRequest, agent_id=agent_id, get_request_kwargs={'params': {'embed': 'customer, message', 'page': 1, 'per_page': 100}})
    else:
        logger.error("Unsupported mode %s" % mode)
        return

    logger.info('Complete: All pages processed')
    if mode == 't':
        for status in TICKET_STATUSES:
            num_tickets = handle_retries(retryable_request=ZendeskVerification, get_request_kwargs={'params': {'query': 'type:ticket status:%s' % status}})
            if not num_tickets:
                logger.error("Verification failed")
                return
            logger.info('Number of %s tickets in Zendesk: %s' % (status, num_tickets))
    else:
        for role in ROLES:
            num_users = handle_retries(retryable_request=ZendeskVerification, get_request_kwargs={'params': {'query': 'type:user role:%s' % role}})
            if not num_users:
                logger.error("Verification failed")
                return
            logger.info('Number of %s users in Zendesk: %s' % (role, num_users))


if __name__ == "__main__":
    main()
