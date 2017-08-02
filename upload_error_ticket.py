import argparse
import logging
import main

from constants import AGENT_ID
from main import flush_queues, get_global_results, migrate_ticket, migrate_user, post_tickets_zendesk, post_users_zendesk
from retryable_request import DeskIndividualCustomerRequest, DeskIndividualTicketRequest, ZendeskUserRequest, handle_retries

logger = logging.getLogger("migrate_to_zendesk")


def get_customers(ticket):
    desk_user_model = handle_retries(retryable_request=DeskIndividualCustomerRequest,
                                     get_request_kwargs={'url': '/api/v2/customers/%s' % ticket.user_id,
                                                         'params': {'embed': 'facebook_user,twitter_user'}})
    logger.info("Adding customer external id %s" % desk_user_model.id)
    migrate_user(desk_user_model)


def main_upload():
    POOL = main.POOL
    global_results = get_global_results()
    parser = argparse.ArgumentParser(description="Migrate leftover support tickets from desk to zendesk.")
    parser.add_argument("--mode", help="Specify either (u)sers or (t)ickets to migrate")
    parser.add_argument('--filename', help="Specify file to read broken tickets from")
    options = parser.parse_args()
    mode = options.mode
    filename = options.filename
    agent_id = handle_retries(retryable_request=ZendeskUserRequest, get_request_kwargs={"url": "/api/v2/users/%s" % AGENT_ID})
    with open(filename) as f:
        desk_ticket_ids = [line.strip() for line in f]

    for i in xrange(0, len(desk_ticket_ids), 100):
        comma_sep_tickets = ','.join(desk_ticket_ids[i: i + 100])
        desk_ticket_models = handle_retries(retryable_request=DeskIndividualTicketRequest,
                                            get_request_kwargs={'url': '/api/v2/cases/search',
                                                                'params': {'embed': 'customer, message', 'page': 1, 'per_page': 100, 'case_id': comma_sep_tickets}})

        for ticket in desk_ticket_models:
            if mode == 'u':
                post_func = post_users_zendesk
                global_results.appendleft(POOL.apply_async(get_customers, kwds={"ticket": ticket}))
            elif mode == 't':
                logger.info("Adding ticket external id %s" % ticket.id)
                post_func = post_tickets_zendesk
                global_results.appendleft(POOL.apply_async(migrate_ticket, kwds={"ticket": ticket, "agent": agent_id}))

    while len(global_results) > 0:
        result = global_results.pop()
        result.get()
    POOL.close()
    POOL.join()

    flush_queues(post_func)


if __name__ == "__main__":
    main_upload()
