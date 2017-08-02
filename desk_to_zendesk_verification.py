import getpass
import logging
import requests

logger = logging.getLogger("zd_d_verifier")
FORMAT = '[%(asctime)s] %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)

ZENDESK_SITE = 'https://mangohealth.zendesk.com'
TICKET_STATUSES = ['open', 'solved', 'closed']
ROLES = ['end-user', 'agent', 'admin']

get_headers = {
    'Accept': 'application/json',
}


def get_tickets(zendesk_auth):
    tickets = {}
    for status in TICKET_STATUSES:
        param = {
            'query': 'type:ticket status:%s' % status
        }
        response = requests.get('%s/api/v2/search.json' % ZENDESK_SITE, params=param, auth=zendesk_auth)
        if response.ok:
            data = response.json()
            logger.info('Number of %s tickets in Zendesk: %s' % (status, data.get('count')))
            tickets[status] = data
        else:
            logger.info('API Failure: %d %s' % (response.status_code, response.headers))
    return tickets


def get_users(zendesk_auth):
    users = {}
    for role in ROLES:
        param = {
            'query': 'type:user role:%s' % role
        }
        response = requests.get('%s/api/v2/search.json' % ZENDESK_SITE, params=param, auth=zendesk_auth)
        if response.ok:
            data = response.json()
            logger.info('Number of %s users in Zendesk: %s' % (role, data.get('count')))
            users[role] = data
        else:
            logger.info('API Failure: %d %s' % (response.status_code, response.headers))
    return users


def main():
    zendesk_auth = ('%s/token' % raw_input('Zendesk email: '),
                    getpass.getpass('Zendesk token: '))
    get_tickets(zendesk_auth)
    get_users(zendesk_auth)


if __name__ == "__main__":
    main()
