"""S2 单元测试 — 利润引擎（TC-UNIT-F-001~004、F-009）

利润公式是重构"不动核心"（README 第六节），本套件与既有 test_profit_golden.py
共同锁定：任何公式改动必须先修改本文件并说明原因。
汇率四级回退（TC-UNIT-F-005~008）需要真实数据，归并到 TC-CON-005 实测。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.profit.engine import DEFAULT_RATES, compute_net_amazon, compute_net_profit


def _D(x):
    return Decimal(str(x))


def _net(ps, pst, pc, sct, gwc, gwct, promo, promo_tax, mkt, sf, fba, adj,
         er, cost=0, freight=0, ad=0, stg=0, ret=0, inb=0, rem=0):
    return compute_net_profit(_D(ps), _D(pst), _D(pc), _D(sct), _D(gwc), _D(gwct),
                              _D(promo), _D(promo_tax), _D(mkt), _D(sf), _D(fba), _D(adj),
                              _D(er), _D(cost), _D(freight), _D(ad), _D(stg),
                              _D(ret), _D(inb), _D(rem))


class TestProfitFormula:
    def test_order_line_golden(self):  # TC-UNIT-F-001（旧系统实测样本）
        args = (_D('23.32'), _D('4.66'), _D('0'), _D('0'), _D('0'), _D('0'),
                _D('0'), _D('0'), _D('0'), _D('-4.67'), _D('-1.96'), _D('0'))
        assert compute_net_amazon(*args) == _D('21.35')
        assert compute_net_profit(*args, _D('9.0'), *[_D('0')] * 7) == _D('192.15')

    def test_full_deductions(self):  # TC-UNIT-F-002（成本/运费/五项费用全扣）
        assert _net('100', '0', '0', '0', '0', '0', '0', '0', '0',
                    '-10', '-6', '0', '6.8',
                    cost=30, freight=10, ad=2, stg=1, ret=0.5, inb=0.3, rem=0.2) \
            == _D('504.00')

    def test_quantize_2dp(self):  # TC-UNIT-F-003
        assert _net('10', *['0'] * 11, '7.5') == _D('75.00')

    def test_refund_dominant_negative(self):  # TC-UNIT-F-004
        na = compute_net_amazon(_D('-50'), _D('0'), _D('0'), _D('0'), _D('0'), _D('0'),
                                _D('0'), _D('0'), _D('0'), _D('-7.5'), _D('-4'), _D('0'))
        assert na == _D('-61.50')
        assert _net('-50', '0', '0', '0', '0', '0', '0', '0', '0',
                    '-7.5', '-4', '0', '9') == _D('-553.50')

    def test_default_rates_baseline(self):  # TC-UNIT-F-009 基线锁定（旧系统逐字一致）
        assert DEFAULT_RATES == {
            'US': '6.8', 'UK': '9.0', 'DE': '7.5', 'FR': '7.5', 'ES': '7.5', 'IT': '7.5',
            'NL': '7.5', 'SE': '0.65', 'BE': '7.5', 'IE': '7.5', 'CA': '5.0', 'MX': '0.4',
            'AE': '1.85', 'SA': '1.81', 'AU': '4.8', 'PL': '1.75',
        }
