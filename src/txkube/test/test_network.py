# Copyright Least Authority Enterprises.
# See LICENSE for details.

"""
Tests for ``txkube.network_kubernetes``.

See ``get_kubernetes`` for pre-requisites.
"""

from os import environ
from base64 import b64encode

from zope.interface import implementer
from zope.interface.verify import verifyClass

import attr

from yaml import safe_dump

from testtools.matchers import AnyMatch, ContainsDict, Equals

from eliot.testing import capture_logging

from OpenSSL.crypto import FILETYPE_PEM

from twisted.test.proto_helpers import MemoryReactor
from twisted.trial.unittest import TestCase as TwistedTestCase

from twisted.python.filepath import FilePath
from twisted.python.url import URL
from twisted.python.components import proxyForInterface
from twisted.internet.ssl import (
    CertificateOptions, DN, KeyPair, trustRootFromCertificates,
)
from twisted.internet.interfaces import IReactorSSL
from twisted.internet.endpoints import SSL4ServerEndpoint
from twisted.web.client import Agent
from twisted.web.server import Site
from twisted.web.resource import Resource
from twisted.web.static import Data

from ..testing import TestCase
from ..testing.integration import kubernetes_client_tests

from .. import (
    IObject, v1, network_kubernetes, network_kubernetes_from_context,
)

from .._network import collection_location


def get_kubernetes(case):
    """
    Create a real ``IKubernetes`` provider, taking necessary
    configuration details from the environment.

    To use this set ``TXKUBE_INTEGRATION_CONTEXT`` to a context in your
    ``kubectl`` configuration.  Corresponding details about connecting to a
    cluster will be loaded from that configuration.
    """
    try:
        context = environ["TXKUBE_INTEGRATION_CONTEXT"]
    except KeyError:
        case.skipTest("Cannot find TXKUBE_INTEGRATION_CONTEXT in environment.")
    else:
        from twisted.internet import reactor
        return network_kubernetes_from_context(reactor, context)


class KubernetesClientIntegrationTests(kubernetes_client_tests(get_kubernetes)):
    """
    Integration tests which interact with a network-accessible
    Kubernetes deployment via ``txkube.network_kubernetes``.
    """



class CollectionLocationTests(TestCase):
    """
    Tests for ``collection_location``.
    """
    def _test_collection_location(self, version, kind, expected, namespace, instance):
        """
        Verify that ``collection_location`` for a particular version, kind,
        namespace, and Python object.

        :param unicode version: The *apiVersion* of the object to test.
        :param unicode kind: The *kind* of the object to test.

        :param tuple[unicode] expected: A representation of the path of the
            URL which should be produced.

        :param namespace: The namespace the Python object is to claim - a
            ``unicode`` string or ``None``.

        :param bool instance: Whether to make the Python object an instance
            (``True``) or a class (``False``)..
        """
        k = kind
        n = namespace
        @implementer(IObject)
        class Mythical(object):
            apiVersion = version
            kind = k

            metadata = v1.ObjectMeta(namespace=n)

            def serialize(self):
                return {}

        verifyClass(IObject, Mythical)

        if instance:
            o = Mythical()
        else:
            o = Mythical

        self.assertThat(
            collection_location(o),
            Equals(expected),
        )


    def test_v1_type(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */api/v1/<kind>s* when called with an ``IObject`` implementation of a
        *v1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1", u"Mythical", (u"api", u"v1", u"mythicals"),
            namespace=None,
            instance=False,
        )


    def test_v1_instance(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */api/v1/namespace/<namespace>/<kind>s* when called with an
        ``IObject`` provider representing Kubernetes object of a *v1* kind.
        """
        self._test_collection_location(
            u"v1", u"Mythical",
            (u"api", u"v1", u"namespaces", u"ns", u"mythicals"),
            namespace=u"ns",
            instance=True,
        )


    def test_v1beta1_type(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */apis/extensions/v1beta1/<kind>s* when called with an ``IObject``
        implementation of a *v1beta1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1beta1", u"Mythical",
            (u"apis", u"extensions", u"v1beta1", u"mythicals"),
            namespace=None,
            instance=False,
        )


    def test_v1beta1_instance(self):
        """
        ``collection_location`` returns a tuple representing an URL path like
        */apis/extensions/v1beta1/<kind>s* when called with an ``IObject``
        implementation of a *v1beta1* Kubernetes object kind.
        """
        self._test_collection_location(
            u"v1beta1", u"Mythical",
            (u"apis", u"extensions", u"v1beta1", u"namespaces", u"ns",
             u"mythicals"),
            namespace=u"ns",
            instance=True,
        )


class ExtraNetworkClientTests(TestCase):
    """
    Direct tests for ``_NetworkClient`` that go beyond the guarantees of
    ``IKubernetesClient``.
    """
    @capture_logging(
        lambda self, logger: self.expectThat(
            logger.messages,
            AnyMatch(ContainsDict({
                u"action_type": Equals(u"network-client:list"),
                u"apiVersion": Equals(u"v1"),
                u"kind": Equals(u"Pod"),
            })),
        ),
    )
    def test_list_logging(self, logger):
        """
        ``_NetworkClient.list`` logs an Eliot event describing its given type.
        """
        client = network_kubernetes(
            base_url=URL.fromText(u"http://127.0.0.1/"),
            agent=Agent(MemoryReactor()),
        ).client()
        client.list(v1.Pod)



class NetworkKubernetesFromContextTests(TwistedTestCase):
    """
    Direct tests for ``network_kubernetes_from_context``.
    """
    def test_client_chain_certificate(self):
        """
        A certificate chain in the *client-certificate* section of in the kube
        configuration file is used to configure the TLS context used when
        connecting to the API server.
        """
        ca_key = KeyPair.generate()
        ca_cert = ca_key.selfSignedCert(1, commonName="ca")

        intermediate_key = KeyPair.generate()
        intermediate_req = intermediate_key.requestObject(DN(commonName="intermediate"))
        intermediate_cert = ca_key.signRequestObject(DN(commonName="ca"), intermediate_req, 1)

        client_key = KeyPair.generate()
        client_req = client_key.requestObject(DN(commonName="client"))
        client_cert = ca_key.signRequestObject(DN(commonName="intermediate"), client_req, 1)

        chain = b"".join([
            client_cert.dumpPEM(),
            intermediate_cert.dumpPEM(),
        ])

        config = self.write_config(ca_cert, chain, client_key)
        kubernetes = lambda reactor: network_kubernetes_from_context(
            reactor, "foo-ctx", path=config,
        )
        return self.check_tls_config(ca_key, ca_cert, kubernetes)


    def check_tls_config(self, ca_key, ca_cert, get_kubernetes):
        """
        Verify that a TLS server configured with the given key and certificate and
        the Kubernetes client returned by ``get_kubernetes`` can negotiate a
        TLS connection.
        """
        # Set up an HTTPS server that requires the certificate chain from the
        # configuration file.  This, because there's no way to pry inside a
        # Context and inspect its state nor any easy way to make Agent talk
        # over an in-memory transport.
        from twisted.internet import reactor
        endpoint = SSL4ServerEndpoint(
            reactor,
            0,
            CertificateOptions(
                privateKey=ca_key.original,
                certificate=ca_cert.original,
                trustRoot=trustRootFromCertificates([ca_cert]),
            ),
        )
        root = Resource()
        root.putChild(b"", Data(b"success", b"text/plain"))

        redirectable = Redirectable(reactor)
        client = get_kubernetes(redirectable).client()
        agent = client.agent

        d = endpoint.listen(Site(root))
        def listening(port):
            self.addCleanup(port.stopListening)
            url = b"https://127.0.0.1:8443/"
            redirectable.set_redirect(port.getHost().host, port.getHost().port)
            return agent.request(b"GET", url)
        d.addCallback(listening)
        return d


    def write_config(self, ca_cert, chain, client_key):
        config = FilePath(self.mktemp())
        config.setContent(safe_dump({
            "apiVersion": "v1",
            "contexts": [
                {
                    "name": "foo-ctx",
                    "context": {
                        "cluster": "foo-cluster",
                        "user": "foo-user",
                    },
                },
            ],
            "clusters": [
                {
                    "name": "foo-cluster",
                    "cluster": {
                        "certificate-authority-data": b64encode(ca_cert.dump(FILETYPE_PEM)),
                        "server": "https://127.0.0.1:8443/",
                    },
                },
            ],
            "users": [
                {
                    "name": "foo-user",
                    "user": {
                        "client-certificate-data": b64encode(chain),
                        "client-key-data": b64encode(client_key.dump(FILETYPE_PEM)),
                    },
                },
            ],
        }))
        return config



@attr.s
class Redirectable(proxyForInterface(IReactorSSL)):
    original = attr.ib()

    def set_redirect(self, host, port):
        self.host, self.port = host, port

    def connectSSL(self, host, port, *a, **kw):
        return self.original.connectSSL(self.host, self.port, *a, **kw)
