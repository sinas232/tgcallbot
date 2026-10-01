"""DNS/UDP probe must not overstate what it can prove about Telegram media."""
from __future__ import annotations

import struct
import unittest
from unittest.mock import AsyncMock, patch

from tools import check_udp


class UdpProbeTests(unittest.TestCase):
    def test_dns_reply_requires_matching_transaction_id_and_response_flag(self):
        ident = 0x1234
        reply = struct.pack('>HHHHHH', ident, 0x8180, 1, 1, 0, 0)
        self.assertTrue(check_udp._dns_response_matches(reply, ('1.1.1.1', 53),
                                                        '1.1.1.1', 53, ident))
        self.assertFalse(check_udp._dns_response_matches(reply, ('2.2.2.2', 53),
                                                         '1.1.1.1', 53, ident))
        self.assertFalse(check_udp._dns_response_matches(reply, ('1.1.1.1', 53),
                                                         '1.1.1.1', 53, 0x4321))
        self.assertFalse(check_udp._dns_response_matches(reply[:5], ('1.1.1.1', 53),
                                                         '1.1.1.1', 53, ident))
        query = struct.pack('>HHHHHH', ident, 0x0100, 1, 0, 0, 0)
        self.assertFalse(check_udp._dns_response_matches(query, ('1.1.1.1', 53),
                                                         '1.1.1.1', 53, ident))


class UdpSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_dns_resolver_is_sufficient_to_prove_some_udp_53_access(self):
        with patch.object(check_udp, 'probe', new_callable=AsyncMock,
                          side_effect=[True, False, False]), \
             patch('builtins.print') as show:
            self.assertEqual(await check_udp.main(), 0)
        text = '\n'.join(' '.join(map(str, call.args)) for call in show.call_args_list)
        self.assertIn('DNS/UDP-53', text)
        self.assertIn('NOT', text)
        self.assertIn('Telegram', text)

    async def test_no_dns_resolver_is_inconclusive_not_proof_all_udp_blocked(self):
        with patch.object(check_udp, 'probe', new_callable=AsyncMock,
                          return_value=False), patch('builtins.print') as show:
            self.assertEqual(await check_udp.main(), 1)
        text = '\n'.join(' '.join(map(str, call.args)) for call in show.call_args_list)
        self.assertIn('inconclusive', text.lower())
        self.assertNotIn('CANNOT work', text)
