import asyncio
import logging
import random
import operator
import functools
from collections import defaultdict
from heapq import nsmallest
from hashlib import sha256

import msgpack
from bases import Bases
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as PublicKey

from qadom.rpcudp import RPCProtocol


log = logging.getLogger(__name__)


REPLICATION_DEFAULT = 5  # TODO: increase
REPLICATION_MAX = 20


def make_uid():
    """Create a wanna-be unique identifier. Return an integer."""
    return random.getrandbits(256)


def nearest(k, peers, uid):
    """Return K nearest to to UID peers in PEERS according to XOR"""
    # XXX: It only works with len(peers) < 10^6 more than that count
    # of peers and the time it takes to compute the nearest peers will
    # timeout after 5 seconds on the other side. See RPCProtocol and
    # Peer.find_peers.
    return nsmallest(k, peers, key=functools.partial(operator.xor, uid))


def pack(integer):
    """Returns a bytes representation of integer in network order"""
    return integer.to_bytes(32, byteorder='big')


def unpack(bytes):
    """Returns an integer"""
    return int.from_bytes(bytes, byteorder='big')


def digest(bytes):
    """Return the sha256 of BYTES as an integer"""
    return unpack(sha256(bytes).digest())


class _Peer:


    def __init__(self, uid, private_key, replication=REPLICATION_DEFAULT):
        assert replication <= REPLICATION_MAX
        # equivalent to k in kademlia, also used as alpha. It specify the
        # how many peers are returned in find_peers, how many peers will
        # receive store calls to store a value and also the number of
        # peers that are contacted when looking up peers in find_peers.
        self.replication = replication
        # bag associates a key with a set of key.  This can be freely
        # set by peers in the network and allows to link a well known
        # key to other keys. It is inspired from gnunet-fs keywords
        # feature. See 'Peer.add' and 'Peer.search'.
        self._bag = defaultdict(set)
        # peers stores the equivalent of the kademlia routing table
        # aka. kbuckets. uid/key to address mapping.
        self._peers = dict()
        # address to uid/key mapping
        self._addresses = dict()
        # blacklist misbehaving nodes. Stores uid/key.
        self._blacklist = set()
        # RPCProtocol set in Peer.listen
        self._protocol = None
        # storage associate a key to a value.  The key must always be
        # the unpacked sha256 of the value.
        self._storage = dict()
        # Set in Peer.listen
        self._transport = None
        # uid (pronouced 'weed') is the identifier of the peer in the
        # overlay network.  It is self-assigned and must be globally
        # unique otherwise some Bad Things can happen. The uid specify
        # loosly depending on the network topology which keys that
        # peer is responsible for. uid must be in the same space as
        # keys that is less than 2^256.
        self._uid = uid

        # for use with namespace

        # ed25519 private key (that includes the public key)
        self._private_key = private_key
        # store key/value pairs per public_key
        self._namespace = defaultdict(dict)

    def __repr__(self):
        return '<_Peer "%r">' % self._uid

    def close(self):
        self._transport.close()

    def blacklist(self, address):
        try:
            uid = self._addresses[address]
        except KeyError:
            pass
        else:
            del self._addresses[address]
            del self._peers[uid]
        self._blacklist.add(address[0])

    async def listen(self, port, interface='0.0.0.0'):
        """Start listening on the given port.

        Provide interface="::" to accept ipv6 address.

        """
        loop = asyncio.get_event_loop()
        listen = loop.create_datagram_endpoint(RPCProtocol, local_addr=(interface, port))
        self._transport, self._protocol = await listen
        # register remote procedures
        self._protocol.register(self.ping)
        self._protocol.register(self.find_peers)
        self._protocol.register(self.find_value)
        self._protocol.register(self.store)
        self._protocol.register(self.add)
        self._protocol.register(self.search)
        self._protocol.register(self.namespace_get)
        self._protocol.register(self.namespace_set)

    async def bootstrap(self, address):
        """Add address to the list of peers.

        Send a ping to ADDRESS and add it with its uid as in the list of
        known peers.

        """
        uid = await self._protocol.rpc(address, 'ping', pack(self._uid))
        uid = unpack(uid)
        self._peers[uid] = address
        self._addresses[address] = uid

    # remote procedures

    async def ping(self, address, uid):
        """Remote procedure that register the remote and returns the uid"""
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return pack(self._uid)
        uid = unpack(uid)
        log.debug("[%r] ping uid=%r from %r", self._uid, uid, address)
        self._peers[uid] = address
        self._addresses[address] = uid
        return pack(self._uid)

    async def find_peers(self, address, uid):
        """Remote procedure that returns peers that are near UID"""
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return [random.randint(2**256) for x in range(self.replication)]
        # The code is riddle with unpack/pack calls because Peer
        # stores key/uid as integer and msgpack doesn't accept such
        # big integers hence it is required to pass them as bytes.
        uid = unpack(uid)
        log.debug("[%r] find peers uid=%r from %r", self._uid, uid, address)
        # XXX: if this takes more than 5 seconds (see RPCProtocol) it
        # will timeout in the other side.
        uids = nearest(self.replication, self._peers.keys(), uid)
        out = [self._peers[uid] for uid in uids]
        return out

    # dict procedures (vanilla dht api)

    async def find_value(self, address, key):
        """Remote procedure that returns the associated value or peers that
        are near KEY"""
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return (b'PEERS', [random.randint(2**256) for x in range(self.replication)])
        log.debug("[%r] find value key=%r from %r", self._uid, key, address)
        try:
            return (b'VALUE', self._storage[unpack(key)])
        except KeyError:
            out = await self.find_peers(None, key)
            return (b'PEERS', out)

    async def store(self, address, value):
        """Remote procedure that stores value locally with its digest as
        key"""
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return True
        log.debug("[%r] store from %r", self._uid, address)
        key = digest(value)
        # check that peer is near the key
        peers = await self.find_peers(None, pack(key))
        peers = nearest(REPLICATION_MAX, peers, key)
        high = peers[-1] ^ key
        current = self._uid ^ key
        if current >= high:
            log.warning('[%r] received a value that is too far from %r', self._uid, address)
            self.blacklist(address)
            # XXX: pretend the value was stored
            return True
        else:
            self._storage[key] = value
            return True

    # bag procedures

    async def add(self, address, key, value):
        """Remote procedure that adds VALUE to the list of uid at KEY"""
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return True
        log.debug("[%r] add key=%r value=%r from %r", self._uid, key, value, address)
        # TODO: do more validation and blacklist if error
        key = unpack(key)
        value = unpack(value)
        if key > 2**256 or value > 2**256:
            self.blacklist(address)
            # XXX: pretend everything is ok
            return True
        else:
            self._bag[key].add(value)
            return True

    async def search(self, address, uid):
        """Remote procedure that returns values associated with KEY if any,
        otherwise return peers near KEY"""
        log.debug("[%r] search uid=%r from %r", self._uid, uid, address)
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return (b'PEERS', [random.randint(2**256) for x in range(self.replication)])

        # TODO: check self is in the perimeter of key
        uid = unpack(uid)
        if uid in self._bag:
            values = [pack(v) for v in self._bag[uid]]
            return (b'VALUES', values)
        else:
            peers = await self.find_peers(None, pack(uid))
            return (b'PEERS', peers)

    # namespace procedures

    async def namespace_set(self, address, public_key, key, value, signature):
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return True
        log.warning('namespace_set form %r', address)
        # TODO: check (public_key, key) is inside self's perimeter
        public = PublicKey.from_public_bytes(public_key)
        try:
            public.verify(signature, msgpack.packb((key, value)))
        except InvalidSignature:
            log.warning('[%r] invalid signature from %r', self._uid, address)
            # XXX: pretend everything is ok
            return True
        else:
            # store it
            self._namespace[public_key][unpack(key)] = value
            return True

    async def namespace_get(self, address, public_key, key):
        if address[0] in self._blacklist:
            # XXX: pretend everything is ok
            return (b'PEERS', [random.randint(2**256) for x in range(self.replication)])

        if public_key in self._namespace:
            try:
                return (b'VALUE', self._namespace[public_key][unpack(key)])
            except KeyError:
                pass
        # key not found, return nearest peers
        uid = digest(msgpack.packb((public_key, key)))
        peers = await self.find_peers(None, pack(uid))
        return (b'PEERS', peers)

    # local methods

    async def get(self, key):
        """Local method to fetch the value associated with KEY

        KEY must be an integer below 2^256"""
        assert key <= 2**256
        try:
            return self._storage[key]
        except KeyError:
            out = await self._get(key)
            return out

    async def _get(self, key):
        """Fetch the value associated with KEY from the network"""
        key = pack(key)
        queried = set()
        while True:
            # retrieve the k nearest peers and remove already queried peers
            peers = await self.find_peers(None, key)
            peers = [address for address in peers if address not in queried]
            # no more peer to query, the key is not found in the dht
            if not peers:
                raise KeyError(unpack(key))
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'find_value', key)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                elif response[0] == b'VALUE':
                    value = response[1]
                    if digest(value) == unpack(key):
                        self._storage[unpack(key)] = value
                        return value
                    else:
                        log.warning('[%r] bad value returned from %r', self._uid, address)
                        self.blacklist(address)
                        continue
                elif response[0] == b'PEERS':
                    # TODO: use gather
                    for address in response[1]:
                        uid = await self._protocol.rpc(address, 'ping', pack(self._uid))
                        uid = unpack(uid)
                        self._peers[uid] = address
                        self._addressed[address] = uid
                else:
                    log.warning('[%r] unknown response %r from %r', self._uid, response[0], address)

    async def set(self, value):
        """Store VALUE in the network.

        Return the uid with which it is associated aka. sha256 integer representation."""
        if len(value) > (8192 - 28):  # datagram max size minus
                                      # "header", see RPCProtocol.
            raise ValueError('value too big')
        key = unpack(sha256(value).digest())
        # unlike kademlia store value locally
        self._storage[key] = value
        # store in the dht, find the nearest peers and call store rpc
        key = pack(key)
        queried = set()
        while True:
            # find peers and remove already queried peers
            peers = await self.find_peers(None, key)
            peers = [address for address in peers if address not in queried]
            # no more peer to query, the nearest peers in the network
            # are known
            if not peers:
                peers = await self.find_peers(None, key)
                queries = [self._protocol.rpc(address, 'store', value) for address in peers]
                # TODO: make sure replication is fullfilled
                await asyncio.gather(*queries, return_exceptions=True)
                return unpack(key)
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'find_peers', key)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                # TODO: use gather
                for address in response:
                    uid = await self._protocol.rpc(address, 'ping', pack(self._uid))
                    uid = unpack(uid)
                    self._peers[uid] = address
                    self._addresses[address] = uid

    # key local method

    async def bag(self, key, value=None):
        """Bag search and publish.

        If VALUE is set, it will append VALUE to KEY in the network.
        If VALUE is NOT set, it will lookup uid associated with KEY in
        the network.

        Both VALUE and KEY must be integers below 2^256.

        """
        if value is None:
            out = await self._search(key)
            return out
        else:
            await self._add(key, value)

    async def _add(self, key, value):
        """Publish VALUE at KEY"""
        key = pack(key)
        value = pack(value)
        # find the nearest peers and call append rpc
        queried = set()
        while True:
            # find peers and remove already queried peers
            peers = await self.find_peers(None, key)
            peers = [address for address in peers if address not in queried]
            # no more peer to query, the nearest peers in the network
            # are known
            if not peers:
                peers = await self.find_peers(None, key)
                queries = [self._protocol.rpc(address, 'add', key, value) for address in peers]
                # TODO: make sure replication is fullfilled
                await asyncio.gather(*queries, return_exceptions=True)
                return
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'find_peers', key)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                for address in response:
                    uid = await self._protocol.rpc(address, 'ping', pack(self._uid))
                    uid = unpack(uid)
                    self._peers[uid] = address
                    self._addresses[address] = uid

    async def _search(self, key):
        """Search values associated with KEY"""
        key = pack(key)
        out = set()
        queried = set()
        while True:
            # retrieve the k nearest peers and remove already queried peers
            peers = await self.find_peers(None, key)
            peers = [address for address in peers if address not in queried]
            # no more peer to query
            if not peers:
                return out
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'search', key)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                elif response[0] == b'VALUES':
                    values = set([unpack(x) for x in response[1]])
                    out = out.union(values)
                elif response[0] == b'PEERS':
                    for address in response[1]:
                        uid = await self._protocol.rpc(address, 'ping', pack(self._uid))
                        uid = unpack(uid)
                        self._peers[uid] = address
                        self._addresses[address] = address
                else:
                    log.warning('[%r] unknown response %r from %r', self._uid, response[0], address)


    # namespace local method

    async def namespace(self, key, value=None, public_key=None, signature=None):
        assert key <= 2**256
        if value is None:
            assert isinstance(public_key, bytes)
            assert isinstance(signature, bytes)
            out = await self._namespace_get(public_key, key, signature)
            return out
        else:
            assert isinstance(value, bytes)
            assert len(value) < 8000  # TODO: compute the real max size
            out = await self._namespace_set(key, value)
            return out

    async def _namespace_get(self, public_key, key, signature):
        key = pack(key)
        uid = pack(digest(msgpack.packb((public_key, key))))
        queried = set()
        while True:
            # retrieve the k nearest peers and remove already queried peers
            peers = await self.find_peers(None, uid)
            peers = [address for address in peers if address not in queried]
            # no more peer to query, the key is not found
            if not peers:
                raise KeyError((unpack(public_key), unpack(key)))
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'namespace_get', public_key, key)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                elif response[0] == b'VALUE':
                    value = response[1]
                    public_key_object = PublicKey.from_public_bytes(public_key)
                    payload = msgpack.packb((key, value))
                    try:
                        public_key_object.verify(signature, payload)
                    except InvalidSignature:
                        self.warning('invalid namespace set form %r', address)
                        self.blacklist(address)
                        continue
                    else:
                        self._namespace[public_key][unpack(key)] = value
                        return value
                elif response[0] == b'PEERS':
                    for address in response[1]:
                        new = await self._protocol.rpc(address, 'ping', pack(self._uid))
                        new = unpack(new)
                        self._peers[new] = address
                        self._addresses[address] = new
                else:
                    log.warning('[%r] unknown response %r from %r', self._uid, response[0], address)

    async def _namespace_set(self, key, value):
        """Publish VALUE at KEY"""
        key = pack(key)
        # compute identifier of the node where to store that (public_key, key, value)
        public_key = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        uid = pack(digest(msgpack.packb((public_key, key))))
        log.critical('uid=%r', unpack(uid))
        # find the nearest peers and call namespace_set rpc
        queried = set()
        while True:
            # find peers and remove already queried peers
            peers = await self.find_peers(None, uid)
            peers = [address for address in peers if address not in queried]
            # no more peer to query, the nearest peers in the network
            # are known
            if not peers:
                # sign pair
                payload = msgpack.packb((key, value))
                signature = self._private_key.sign(payload)
                # call rpc in nearest peers
                peers = await self.find_peers(None, uid)
                queries = []
                for address in peers:
                    query = self._protocol.rpc(
                        address,
                        'namespace_set',
                        public_key,
                        key,
                        value,
                        signature
                    )
                    queries.append(query)
                # TODO: make sure replication is fullfilled
                await asyncio.gather(*queries, return_exceptions=True)
                return signature
            # query selected peers
            queries = []
            for address in peers:
                query = self._protocol.rpc(address, 'find_peers', uid)
                queries.append(query)
            responses = await asyncio.gather(*queries, return_exceptions=True)
            for (response, address) in zip(responses, peers):
                queried.add(address)
                if isinstance(response, Exception):
                    continue
                for address in response:
                    new = await self._protocol.rpc(address, 'ping', pack(self._uid))
                    new = unpack(new)
                    self._peers[new] = address
                    self._addresses[address] = new


async def make_peer(uid, port, private_key=None):
    """Create a peer at PORT with UID as identifier"""
    if private_key is None:
        private_key = PrivateKey.generate()
    peer = _Peer(uid, private_key)
    await peer.listen(port)
    return peer
