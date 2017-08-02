# Desk.com to Zendesk Ticket and User Migration

## Motivation
These scripts will migrate help desk history from Desk.com's system to Zendesk's system, including all ticket information and associated customer information. Neither system has an easy export for tickets or customers to CSV, so we used the Desk API to get raw ticket information, created Desk data models, converted the intermediate models to Zendesk data models, then JSON, and then used the Zendesk API to post all of our data. 

The data transfer is relatively fast (less downtime for support) and is idempotent, so can be safely run multiple times in case of failure.

## How to Install
Clone the script repo, and run the following commands to install necessary packages (if you don't already have them). You'll need Python 2.7. Install pip and setup-tools (https://packaging.python.org/tutorials/installing-packages/#install-pip-setuptools-and-wheel)

- ```pip install -U -r requirements.txt```


## How to Use
1. In ```constants.py``` change ```AGENT_ID``` to the **Zendesk** user id for the agent you want all old tickets to come from. You can find the agent id by going to your Zendesk agent view, finding the agent user and copying the ID from the url. ```https://my-support-site.zendesk.com/agent/users/AGENT_ID/```

    In ```constants.py``` replace ```my-support-site``` in ```DESK_SITE``` and ```ZENDESK_SITE``` with your help center sites.

    *Optional* In ```constants.py```, adjust ```MAX_RETRIES```, ```PROCESSES```, and ```DEFAULT_WAIT_TIME``` as you see fit.
  
    We chose the value for ```PROCESSES``` by tracking CPU usage and the rate of timeouts - with 100 processes, we would hit the Desk ratelimit within 0-20 seconds.

    ```DEFAULT_WAIT_TIME``` is the fallback time for retrying if we can't read it from the response header. We defaulted this to 60 seconds because rate limits are metered each minute.
    

2. In Zendesk admin, verify all Zendesk triggers and automations to email users are off, and will not act on these migrated tickets.
3. Run ```python main.py --mode u``` first to migrate all of your users. You MUST migrate users before migrating tickets, because tickets are linked to users.
  
    To authenticate, you need an admin username and password for Desk.com, and an admin username and API token for Zendesk.com. Generate a Zendesk token at **Admin > Channels > API**. The user **must** have an admin role.

4. Run ```python main.py --mode t``` second to migrate all of your tickets.
5. If you search your log files and notice some tickets where the creator ID couldn't be found, that's probably because some users were not able to be posted. Check the status of some of your Zendesk user posting jobs using the **Zendesk Jobs Statuses** API (indicated by Job ID: #### in logs) to see errors.
6. Collect a list of ids for tickets that couldn't be posted ("Could not get creator\_id") and save them to a file, one per line ```BROKEN_IDS```
7. Run ```python upload_error_ticket.py --mode u --filename BROKEN_IDS``` if you have users that weren't posted.
8. Run ```python upload_error_ticket.py --mode t --filename BROKEN_IDS``` if you have tickets that weren't posted.

## Caveats

### What it doesn't migrate
This script does not migrate the following fields from Desk.com, because we didn't use them in our setup:

- Brands
- Companies
- Custom Fields
- Feedback
- Filters 
- Groups
- Labels (we copied these over by hand)
- Macros (we copied these over by hand)
- Rules
- Site Settings
- Snippets

To add one or more of these fields, add a RetryableRequest class for the field you want to migrate, add a model for the Desk and Zendesk data, and then create a posting function to post these fields. Using Zendesk's create or update API endpoints is recommended.


### Assumptions

- This script only uses one user who is the agent for all old tickets. This reduces the number of API calls to get user IDs, but if that distinction between agents is important, you'll have to get the user_id of that agent.

- Desk comments that are in draft stage will not be migrated.

- If comments are edited in Desk between runs of the script, edits will not appear in Zendesk.

- The script adds the Desk user ID to all newly created users in Zendesk to the ```external_ids``` field.

- The script tags all new tickets with ```from_desk``` and new users with ```desk_user_before_DATE``` in Zendesk, where DATE is when you run the script. 

### Caution
- If you set up admins, agents or light agents in Zendesk before migration, and if you have tickets from these user's email addresses in Desk, they'll be converted to end-users. You'll have to manually switch them to their appropriate role using curl once the migration is complete. 
- Zendesk calls the Twitter API to add Twitter users to the system. The Twitter integration will silently fail if you end up hitting their ratelimit. Use the ```upload_error_ticket.py``` script to catch all tickets that weren't posted and repost the users, and then the tickets.
- Zendesk won't properly link imported Facebook users to their Facebook account in their user profile page.
- You can simplify the migration by moving ticket creation to Zendesk (route your support channels to point there) and close all Desk.com tikets before running the migration script, to make sure you don't miss any tickets that come in while the script is running.