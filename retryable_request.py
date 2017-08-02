import getpass
import logging
import requests
import time

from constants import DEFAULT_WAIT_TIME, DESKSITE, MAX_RETRIES, ZENDESK_SITE
from zendesk_desk_models import Attachment, FBUser, Message, Ticket, TwitUser, User

GET_HEADERS = {
    'Accept': 'application/json',
}

UPLOAD_HEADERS = {
    'Content-Type': 'application/binary',
}

POST_HEADERS = {
    'Content-Type': 'application/json',
}

logger = logging.getLogger("migrate_to_zendesk")

# Didn't want to implement metaclasses so chose to use module-level authentication.
desk_auth = (raw_input('Desk email: '),
             getpass.getpass('Desk password: '))

zendesk_auth = ('%s/token' % raw_input('Zendesk email: '),
                getpass.getpass('Zendesk token: '))


class RetryableRequest(object):

    method = "get"
    params = {}
    headers = {}
    auth = tuple()
    wait_resp_header = ""

    @classmethod
    def get_request(cls, url=None, data=None, params=None):
        if not data:
            data = {}
        if not params:
            params = {}
        if not url:
            new_url = cls.url
        else:
            new_url = "%s%s" % (cls.url, url)

        return requests.Request(method=cls.method, url=new_url, data=data, params=params, headers=cls.headers, auth=cls.auth)

    @classmethod
    def on_success(cls, resp):
        raise NotImplemented("on_success must be implemented")

    @classmethod
    def on_failure(cls, request, resp):
        return "Headers %s using %s on %s with %s and %s" % (resp.headers, request.method, request.url, request.params, request.data)


class DeskRequest(RetryableRequest):
    wait_resp_header = 'X-Rate-Limit-Reset'
    auth = desk_auth
    headers = GET_HEADERS


class ZendeskRequest(RetryableRequest):
    auth = zendesk_auth
    wait_resp_header = 'retry-after'


def desk_customer_to_schematics(entry, embedded_key):
    desk_user = User(entry, strict=False)
    twitter_user_json_str = entry.get(embedded_key, {}).get("twitter_user", "")
    if twitter_user_json_str:
        desk_user.twitter_user = TwitUser(twitter_user_json_str, strict=False)
    facebook_user_json_str = entry.get(embedded_key, {}).get("facebook_user", "")
    if facebook_user_json_str:
        desk_user.facebook_user = FBUser(facebook_user_json_str, strict=False)
    return desk_user


class DeskCustomerRequest(DeskRequest):
    url = "%s/api/v2/customers" % DESKSITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        user_list = []
        for entry in data.get('_embedded', {}).get('entries', []):
            user_list.append(desk_customer_to_schematics(entry, "_embedded"))
        return user_list


class DeskIndividualCustomerRequest(DeskCustomerRequest):
    url = DESKSITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        return desk_customer_to_schematics(data, "_links")


class DeskTicketRequest(DeskRequest):
    url = "%s/api/v2/cases" % DESKSITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        ticket_obj = []
        for entry in data.get('_embedded', {}).get('entries', []):
            ticket = Ticket(entry, strict=False)
            ticket.user_id = entry.get('_embedded', {}).get('customer', {}).get('id', '')
            ticket.num_attachments = entry.get('_links', {}).get('attachments', {}).get('count', 0)
            ticket.num_replies = entry.get('_links', {}).get('replies', {}).get('count', 0)
            ticket.num_notes = entry.get('_links', {}).get('notes', {}).get('count', 0)
            ticket.messages = []
            first_message = Message(entry.get('_embedded', {}).get('message', {}), strict=False)
            if first_message:
                first_message.direction = 'in'
                first_message.creator_id = ticket.user_id
                ticket.messages.append(first_message)
            ticket_obj.append(ticket)
        return ticket_obj


class DeskIndividualTicketRequest(DeskTicketRequest):
    url = DESKSITE


class CheckUpload(RetryableRequest):
    url = ""
    auth = desk_auth

    @classmethod
    def on_success(cls, response):
        logger.info("Successfully got image")
        return response.content


class ZendeskUpload(ZendeskRequest):
    method = 'post'
    headers = UPLOAD_HEADERS
    url = "%s/api/v2/uploads.json" % ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        return response.json()


class ZendeskPostRequest(ZendeskRequest):
    method = 'post'
    headers = POST_HEADERS

    @classmethod
    def on_success(cls, response):
        logger.info("Successfully posted - posted tickets or users")
        logger.info("Job ID %s" % response.json().get('job_status', {}).get('id', 0))


class ZendeskUserPostRequest(ZendeskPostRequest):
    url = "%s/api/v2/users/create_or_update_many.json" % ZENDESK_SITE


class ZendeskTicketPostRequest(ZendeskPostRequest):
    url = "%s/api/v2/imports/tickets/create_many.json" % ZENDESK_SITE


class ZendeskUpdateRequest(ZendeskRequest):
    method = 'put'
    headers = POST_HEADERS
    url = "%s/api/v2/tickets/update_many.json" % ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        logger.info("Successfully posted - updated tickets")


class DeskMessageRequest(DeskRequest):
    url = DESKSITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        message_list = []
        for entry in data.get('_embedded', {}).get('entries', []):
            message = Message(entry, strict=False)
            if message.status == 'draft':
                continue
            message.uri = entry.get('_links', {}).get('self', {}).get('href', '')
            path = entry.get('_links', {}).get('customer', {}).get('href', '').split('/')
            # url: "/api/v2/customer/<ID>" - we get ID
            if message.direction == 'in':
                if len(path) == 5:
                    try:
                        message.creator_id = int(path[4])
                    except ValueError:
                        logger.info("Could not find creator ID of message - not posting message %s" % entry)
                else:
                    logger.info("Could not find creator ID of message - not posting message %s" % entry)
            message_list.append(message)
        return message_list


class DeskAttachmentRequest(DeskRequest):
    url = DESKSITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        attachment_list = []
        for entry in data.get('_embedded', {}).get('entries', []):
            attachment = Attachment(entry, strict=False)
            attachment.message_uri = entry.get('_links', {}).get('reply', {}).get('href', '')
            attachment_list.append(attachment)
        return attachment_list


class ZendeskTicketIDRequest(ZendeskRequest):
    headers = GET_HEADERS
    url = ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        if data.get('count', 0) == 1:
            return data.get("results", {})[0].get('id', 0)
        elif data.get('count', 0) > 1:
            logger.info("Too many tickets with same external ID")
            return -1
        else:
            return 0


class ZendeskTicketCommentCount(ZendeskRequest):
    headers = GET_HEADERS
    url = ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        return data.get("ticket", {}).get("comment_count", 0)


class ZendeskUserRequest(ZendeskRequest):
    headers = GET_HEADERS
    url = ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        return data.get("user", {}).get('id', 0)


class ZendeskSearch(ZendeskRequest):
    url = ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        if data['count'] == 0:
            return 0
        return data['results'][0].get('id', 0)


class ZendeskVerification(ZendeskRequest):
    url = "%s/api/v2/search.json" % ZENDESK_SITE

    @classmethod
    def on_success(cls, response):
        data = response.json()
        return data.get('count', -1)

def handle_retries(retryable_request, get_request_kwargs=None, remaining_retries=MAX_RETRIES, get_pages=False):  # noqa
    # Create a new session each time to be threadsafe/allow more connections
    session = requests.Session()
    request = retryable_request.get_request(**get_request_kwargs)
    try:
        resp = session.send(request.prepare())
    except requests.exceptions.Timeout:
        if remaining_retries <= 0:
            logger.error("Ran out of retries for %s" % retryable_request)
            return
        logger.info("Sleeping for %d" % DEFAULT_WAIT_TIME)
        time.sleep(DEFAULT_WAIT_TIME)
        return handle_retries(retryable_request=retryable_request,
                              get_request_kwargs=get_request_kwargs,
                              remaining_retries=remaining_retries - 1)
    except requests.exceptions.RequestException:
        logger.exception("Caught unexpected exception for %s" % retryable_request)
        return
    except:
        logger.exception("Caught unexpected exception for %s" % retryable_request)
        return
    if resp.ok:
        # For first API call to get total pages in desk/zendesk
        if get_pages:
            entries = resp.json().get('total_entries')
            logger.info('Total entries to process: %d' % entries)
            return (int(entries) / 100) + 1
        return retryable_request.on_success(resp)
    if resp.status_code == 429:
        if remaining_retries <= 0:
            logger.error("Ran out of retries for %s" % retryable_request)
            return
        time_to_sleep = float(resp.headers.get(retryable_request.wait_resp_header, DEFAULT_WAIT_TIME))
        logger.info("Sleeping for %d" % time_to_sleep)
        time.sleep(time_to_sleep)
        return handle_retries(retryable_request=retryable_request,
                              get_request_kwargs=get_request_kwargs,
                              remaining_retries=remaining_retries - 1)
    else:
        logger.exception("Unhandled status code %d for %s" % (resp.status_code, retryable_request.on_failure(request, resp)))
        return
