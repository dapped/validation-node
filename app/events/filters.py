import functools
import logging

from web3.utils.contracts import find_matching_event_abi
from web3.utils.events import get_event_data

from database import events
from ethereum import rewards

logger = logging.getLogger('flask.app')

STATE_TRANSITION_FILTER = 'StateTransition'
JOIN_EVENT_FILTER = 'JoinEvent'
ERROR_EVENT_FILTER = 'Error'
VALIDATION_STARTED_FILTER = 'ValidationStarted'
VALIDATION_RESTARTED_FILTER = 'ValidationRestart'

EVENT_FILTERS = [JOIN_EVENT_FILTER, STATE_TRANSITION_FILTER, ERROR_EVENT_FILTER,
                 VALIDATION_STARTED_FILTER, VALIDATION_RESTARTED_FILTER]


def log_entry_formatters(contract_abi):
    formatters = {}
    for filter_name in EVENT_FILTERS:
        formatter = functools.partial(get_event_data,
                                      find_matching_event_abi(contract_abi, filter_name))
        formatters[filter_name] = formatter
    return formatters


def init_event_filters(w3, contract_abi, event_id):
    contract_instance = w3.eth.contract(address=event_id, abi=contract_abi)
    for filter_name in EVENT_FILTERS:
        logger.info('Initializing %s filter for %s event', filter_name, event_id)
        filter_ = contract_instance.events[filter_name].createFilter(
            fromBlock='earliest', toBlock='latest')
        events.Filters.create(event_id, filter_.filter_id)
        logger.info('Requesting all entries for %s filter for %s event', filter_name, event_id)
        entries = filter_.get_all_entries()
        if not entries:
            continue
        process_entries(w3, filter_name, event_id, entries)


def filter_events(w3, formatters):
    '''filter_events runs in a cron job and requests new entries for all events'''
    event_ids = events.VerityEvent.get_ids_list()
    for event_id in event_ids:
        filter_ids = events.Filters.get_list(event_id)
        for filter_name, filter_id in zip(EVENT_FILTERS, filter_ids):
            filter_ = w3.eth.filter(filter_id=filter_id)
            filter_.log_entry_formatter = formatters[filter_name]
            entries = filter_.get_new_entries()
            if not entries:
                continue
            process_entries(filter_name, event_id, entries)


def process_entries(w3, filter_name, event_id, entries):
    if filter_name == JOIN_EVENT_FILTER:
        process_join_events(event_id, entries)
    elif filter_name == STATE_TRANSITION_FILTER:
        process_state_transition(event_id, entries)
    elif filter_name == ERROR_EVENT_FILTER:
        process_error_events(event_id, entries)
    elif filter_name == VALIDATION_STARTED_FILTER:
        process_validation_start(event_id, entries)
    elif filter_name == VALIDATION_RESTARTED_FILTER:
        process_validation_round_restart(w3, event_id, entries)
    else:
        logger.error('Unknown event name for event_id %s, %s', event_id, filter_name)


def process_join_events(event_id, entries):
    logger.info('Adding %d %s entries for %s event', len(entries), JOIN_EVENT_FILTER, event_id)
    participants = [entry['args']['wallet'] for entry in entries]
    events.Participants.create(event_id, participants)


def process_state_transition(event_id, entries):
    event = events.VerityEvent.get(event_id)
    entry = entries[0]
    event.state = entry['args']['newState']
    logger.info('Event %s state transition detected. New state %d', event_id, event.state)
    event.update()


def process_validation_start(event_id, entries):
    entry = entries[0]
    event = events.VerityEvent.get(event_id)

    validation_round = entry['args']['validationRound']
    event.rewards_validation_round = validation_round
    event.update()

    if not event.is_master_node:
        rewards.validate_rewards(event_id, validation_round)


def process_validation_round_restart(w3, event_id, entries):
    entry = entries[0]
    event = events.VerityEvent.get(event_id)

    validation_round = entry['args']['validationRound']
    # validation round starts from 1, instead of 0
    is_master_node = w3.eth.defaultAccount == event.node_addresses[validation_round - 1]

    event.rewards_validation_round = validation_round
    event.is_master_node = is_master_node
    event.update()

    # if node is master node, set consensus rewards
    if is_master_node:
        # TODO should this be a scheduler job
        rewards.set_consensus_rewards(event_id)


def process_error_events(event_id, entries):
    for entry in entries:
        logger.error('event_id: %s, %s', event_id, entry)
