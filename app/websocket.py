import asyncio
import json
import logging
import threading

import janus
import websockets


import common
import scheduler
from database import database
from events import consensus

LOOP = asyncio.get_event_loop()
QUEUE = janus.Queue(loop=LOOP)

logger = logging.getLogger()


class Common:
    WEBSOCKETS = dict()  # key: ip:port, value: websocket connection

    @staticmethod
    def key(host, port):
        return '%s:%s' % (host, port)

    @classmethod
    def register(cls, websocket):
        cls.WEBSOCKETS[cls.key(websocket.host, websocket.port)] = websocket

    @classmethod
    def unregister(cls, websocket):
        cls.WEBSOCKETS.pop(cls.key(websocket.host, websocket.port), None)

    @classmethod
    async def connect_to_websocket(cls, address):
        try:
            websocket = await websockets.connect('ws://' + address, timeout=2)
        except Exception as e:
            logger.error('Cannot connect to websocket: %s', address)
            logger.error(e)
            return
        cls.register(websocket)

    @classmethod
    async def get_or_create_websocket_connection(cls, websocket_address):
        if websocket_address not in cls.WEBSOCKETS:
            await cls.connect_to_websocket(websocket_address)
        return cls.WEBSOCKETS.get(websocket_address)

    @classmethod
    async def get_or_create_websocket_connections(cls, node_websocket_ips_ports):
        my_websocket_ip_port = common.node_websocket_ip_port()
        websockets_nodes = []
        for websocket_ip_port in node_websocket_ips_ports:
            if my_websocket_ip_port == websocket_ip_port:
                continue
            websocket = await cls.get_or_create_websocket_connection(websocket_ip_port)
            if not websocket:
                continue
            websockets_nodes.append(websocket)
        return websockets_nodes


class Producer(Common):
    ''' Propagate votes to other nodes'''

    @staticmethod
    def create_message(vote):
        message_send = {'vote': vote.to_json()}
        return json.dumps(message_send)

    @classmethod
    async def producer(cls, message):
        node_websocket_ips = message['node_websocket_ips']
        if not node_websocket_ips:
            logger.warning('Node Websocket IPs are not set')
            return
        websockets_nodes = await cls.get_or_create_websocket_connections(node_websocket_ips)
        if websockets_nodes:
            message_json = cls.create_message(message['vote'])
            await asyncio.wait([websocket.send(message_json) for websocket in websockets_nodes])

    @classmethod
    async def producer_handler(cls, async_q):
        while True:
            message = await async_q.get()
            if 'node_websocket_ips' not in message or 'vote' not in message:
                logger.error('Message does not have required properties: %s', message)
                continue
            await cls.producer(message)


class Consumer(Common):
    ''' Consume votes from other nodes'''

    @staticmethod
    def is_message_valid(message):
        return 'vote' in message

    @staticmethod
    def json_to_vote(vote_json):
        try:
            vote = database.Vote.from_json(vote_json)
        except Exception as e:
            logger.exception(e)
            return None
        return vote

    @staticmethod
    async def event_exists(event_id):
        event = database.VerityEvent.get(event_id)
        return event is not None

    @staticmethod
    async def create_vote(vote):
        vote.create()
        logger.info('[%s] Accepted vote from %s user from %s node: %s', vote.event_id, vote.user_id,
                    vote.node_id, vote.answers)

    @staticmethod
    def should_calculate_consensus(event_id):
        event = database.VerityEvent.get(event_id)
        vote_count = database.Vote.count(event_id)
        if consensus.should_calculate_consensus(event, vote_count):
            event_metadata = event.metadata()
            scheduler.scheduler.add_job(consensus.check_consensus, args=[event, event_metadata])

    @classmethod
    async def consumer(cls, message_json):
        message = json.loads(message_json)
        if not cls.is_message_valid(message):
            logger.error("Message is not valid: %s", message)
            return
        if not common.is_vote_signed(message):
            logger.error("Message is not signed: %s", message)
            return
        vote = cls.json_to_vote(message['vote'])
        if vote is None:
            logger.error("Vote %s from node is not valid", vote.node_id)
            return
        if not await cls.event_exists(vote.event_id):
            logger.error("Event %s does not exist", vote.event_id)
            return
        await cls.create_vote(vote)
        cls.should_calculate_consensus(vote.event_id)

    @classmethod
    async def consumer_handler(cls, websocket, _):
        logger.info('Websocket opened %s:%s', websocket.host, websocket.port)
        while True:
            try:
                message_json = await asyncio.wait_for(websocket.recv(), timeout=20)
            except websockets.exceptions.ConnectionClosed:
                logger.error('Websocket connection closed %s:%s', websocket.host, websocket.port)
                return
            except asyncio.TimeoutError:
                # No data in 20 seconds
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=10)
                except asyncio.TimeoutError:
                    logger.error(
                        'No response to ping in 10 seconds. Websocket connection closed %s:%s',
                        websocket.host, websocket.port)
                    return
            else:
                await cls.consumer(message_json)


def loop_in_thread(event_loop):
    node_websocket_port = common.node_websocket_port()
    asyncio.set_event_loop(event_loop)
    event_loop.run_until_complete(
        websockets.serve(Consumer.consumer_handler, '0.0.0.0', node_websocket_port))
    event_loop.run_until_complete(Producer.producer_handler(QUEUE.async_q))
    event_loop.run_forever()


def init():
    logger.info('Websocket Init started')
    t = threading.Thread(target=loop_in_thread, args=(LOOP, ))
    t.start()
    logger.info('Websocket Init done')
