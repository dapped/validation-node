import logging

import scheduler
from database import database
from ethereum import rewards
from ethereum.provider import NODE_WEB3

logger = logging.getLogger()


def should_calculate_consensus(event, vote_count):
    '''Heuristic which checks if there is a potential for consensus (assumes all votes are valid)'''
    event_id = event.event_id
    participant_ratio = (vote_count / len(event.participants())) * 100
    if vote_count < event.min_total_votes:
        logger.info('[%s] Should not calculate consensus: vote_count<min_total_votes: %d<%d',
                    event_id, vote_count, event.min_total_votes)
        return False
    if participant_ratio < event.min_participant_ratio:
        logger.info(
            '[%s] Should not calculate consensus: participant_ratio<min_participant_ratio: %.4f<%.4f',
            event_id, participant_ratio, event.min_participant_ratio)
        return False
    logger.info('[%s] Should calculate consensus', event_id)
    return True


def check_consensus(event, event_metadata):
    event_id = event.event_id
    votes_by_users = event.votes()
    vote_count = len(votes_by_users)

    if not should_calculate_consensus(event, vote_count):
        return
    consensus_votes_by_users = calculate_consensus(event, votes_by_users)
    if not consensus_votes_by_users:
        logger.info('[%s] Consensus not reached', event_id)
        return
    logger.info('[%s] Consensus reached', event_id)
    if event.metadata().is_consensus_reached:
        logger.info('[%s] Consensus already set', event_id)
        return
    event_metadata.is_consensus_reached = True
    event_metadata.update()

    ether_balance, token_balance = event.instance(NODE_WEB3, event_id).functions.getBalance().call()
    rewards.determine_rewards(event, consensus_votes_by_users, ether_balance, token_balance)
    if event.is_master_node:
        scheduler.scheduler.add_job(rewards.set_consensus_rewards, args=[NODE_WEB3, event_id])
    else:
        logger.info('[%s] Not a master node. Waiting for rewards to be set.', event_id)


def calculate_consensus(event, votes_by_users):
    vote_count = len(votes_by_users)
    if vote_count < event.min_total_votes:
        logger.info(
            '[%s] Not enough valid votes to calculate consensus: vote_count<event.min_total_votes: %d<%d',
            event.event_id, vote_count, event.min_total_votes)
        return dict()

    votes_by_repr = database.Vote.group_votes_by_representation(votes_by_users)
    vote_repr = max(votes_by_repr, key=lambda x: len(votes_by_repr[x]))
    consensus_user_ids = {vote.user_id for vote in votes_by_repr[vote_repr]}
    consensus_votes_by_users = {
        user_id: votes
        for user_id, votes in votes_by_users.items() if user_id in consensus_user_ids
    }

    consensus_votes_count = len(consensus_votes_by_users)
    consensus_ratio = consensus_votes_count / len(votes_by_users)
    if consensus_votes_count < event.min_consensus_votes:
        logger.info(
            '[%s] Not enough consensus votes: consensus_votes_count<event.min_consensus_votes: %d<%d',
            event.event_id, consensus_votes_count, event.min_consensus_votes)
        return dict()
    if consensus_ratio * 100 < event.min_consensus_ratio:
        logger.info(
            '[%s] Not enough consensus votes: consensus_ratio*100<event.min_consensus_ratio: %d<%d',
            event.event_id, consensus_ratio * 100 < event.min_consensus_ratio)
        return dict()
    consensus_vote = votes_by_users[next(iter(consensus_user_ids))][0]
    consensus_vote.set_consensus_vote()
    return consensus_votes_by_users
