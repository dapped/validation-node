import json
import logging
import os
import time
from enum import Enum

from eth_account.messages import defunct_hash_message
from web3 import Web3
from web3.auto import w3 as w3_auto
from web3.gas_strategies.time_based import medium_gas_price_strategy

from database import database
from ethereum.provider import EthProvider

logger = logging.getLogger()

CHUNK_SIZE = 20
GAS_PRICE_FACTOR = 1.2
WAIT_FOR_TRANSACTION_RECEIPT_TIMEOUT = 60 * 15  # 15 minutes


class AddressType(Enum):
    IP = 1
    WEBSOCKET = 2


def verity_event_contract_abi():
    return json.loads(open(os.path.join(os.getenv('CONTRACT_DIR'),
                                        'VerityEvent.json')).read())['abi']


def event_registry_contract_abi():
    return json.loads(open(os.path.join(os.getenv('CONTRACT_DIR'),
                                        'EventRegistry.json')).read())['abi']


def node_registry_contract_abi():
    return json.loads(open(os.path.join(os.getenv('CONTRACT_DIR'),
                                        'NodeRegistry.json')).read())['abi']


def contract_registry_contract_abi():
    return json.loads(
        open(os.path.join(os.getenv('CONTRACT_DIR'), 'ContractRegistry.json')).read())['abi']


def contract_registry_address():
    return Web3.toChecksumAddress(os.getenv('CONTRACT_REGISTRY_ADDRESS'))


def node_id():
    return Web3.toChecksumAddress(os.getenv('NODE_ADDRESS'))


def node_ip():
    return os.getenv('NODE_IP')


def node_port():
    return os.getenv('HTTP_PORT')


def protocol():
    prefix = 'http://'
    if os.getenv('USE_HTTPS') == 'true':
        prefix = 'https://'
    return prefix


def node_ip_port():
    return '%s%s:%s' % (protocol(), node_ip(), node_port())


def node_websocket_port():
    return os.getenv("WEBSOCKET_PORT")


def node_websocket_ip_port():
    return 'ws://%s:%s' % (node_ip(), node_websocket_port())


def explorer_ip():
    return os.getenv('EXPLORER_IP')


def explorer_port():
    return os.getenv('EXPLORER_PORT')


def explorer_ip_port():
    return '%s:%s' % (explorer_ip(), explorer_port())


def function_transact(w3, contract_function, max_retries=3):
    account = EthProvider.account_dict()

    account['address'] = Web3.toChecksumAddress(account['address'])
    next_nonce = w3.eth.getTransactionCount(account['address'])

    for attempt in range(max_retries):
        try:
            raw_txn = _raw_transaction(w3, contract_function, account, next_nonce + attempt)
            tx_receipt = w3.eth.waitForTransactionReceipt(
                raw_txn, timeout=WAIT_FOR_TRANSACTION_RECEIPT_TIMEOUT)
            logger.info('Transmitted transaction %s', Web3.toHex(tx_receipt['transactionHash']))
            return tx_receipt['transactionHash']
        except Exception as e:
            logger.exception('Transaction failed. Retry: %d/%d', attempt, max_retries)
            time.sleep(1)


def _raw_transaction(w3, contract_function, account, nonce):
    w3.eth.setGasPriceStrategy(medium_gas_price_strategy)

    transaction = {
        'from': account['address'],
        'nonce': nonce,
    }
    transaction['gasPrice'] = int(w3.eth.generateGasPrice() * GAS_PRICE_FACTOR)
    transaction['gas'] = 2000000
    signed_txn = w3.eth.account.signTransaction(
        contract_function.buildTransaction(transaction), private_key=account['pvt_key'])
    raw_txn = w3.eth.sendRawTransaction(signed_txn.rawTransaction)
    return raw_txn


def list_to_chunks(list_, chunk_size=CHUNK_SIZE):
    """ Converts a list to chunks with chunk_size entries """
    return list(list_[i:i + chunk_size] for i in range(0, len(list_), chunk_size))


def lists_to_chunks(*lists, batch_size=CHUNK_SIZE):
    """ Converts multiple lists to chunks with chunk_size entries """
    assert len({len(list_) for list_ in lists}) == 1, "Lists have different lengths"

    chunks = list(list_to_chunks(list_, batch_size) for list_ in lists)
    return list(map(list, zip(*chunks)))  # transpose lists


def is_vote_payload_valid(data):
    if data is None:
        return False
    for field in ['data', 'signedData']:
        if field not in data or data[field] is None:
            return False
    for param in {'user_id', 'task_id', 'answers'}:
        if param not in data['data'] or data['data'][param] is None:
            return False
    for answer in data['data']['answers']:
        for key in [database.Vote.ANSWERS_VALUE_KEY, database.Vote.ANSWERS_SORT_KEY]:
            if key not in answer:
                return False
    return True


def parse_fields_from_json_data(json_data):
    data = json_data['data']
    event_id = data['task_id']
    user_id = data['user_id']
    signature = json_data['signedData']
    return event_id, user_id, data, signature


def is_voting_active(timestamp, event_start_time, event_end_time):
    return event_start_time <= timestamp <= event_end_time


def is_user_registered(user_id, event_id):
    return database.Participants.exists(event_id, user_id)


def is_vote_signed(vote_json):
    try:
        vote_data = vote_json['data']
        message = json.dumps(vote_data, separators=(',', ':'))
        data_msg = defunct_hash_message(text=str(message))
        signer = w3_auto.eth.account.recoverHash(data_msg, signature=vote_json['signedData'])
    except Exception as e:
        logger.info(e)
        return False, 'None'
    return vote_data['user_id'] == signer, signer


def from_bytes32_to_str(bytes32):
    bytes32 = bytes32.hex().rstrip("0")
    if len(bytes32) % 2 != 0:
        bytes32 = bytes32 + '0'
    return bytes.fromhex(bytes32).decode('utf8')


def consensus_answers_from_contract(verity_event_instance):
    event_consensus_answers = verity_event_instance.functions.getResults().call()
    if event_consensus_answers is None:
        return None
    return [from_bytes32_to_str(x) for x in event_consensus_answers]


def default_eth_address():
    # default address on smart contract
    return "0x0000000000000000000000000000000000000000"


def sign_data(data):
    message = json.dumps(data, separators=(',', ':'))
    message_hash = defunct_hash_message(text=str(message))
    signature = w3_auto.eth.account.signHash(
        message_hash, private_key=os.getenv('NODE_PRIVATE_KEY'))
    return signature['signature'].hex()