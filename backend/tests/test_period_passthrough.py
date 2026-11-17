"""S21 归月参数透传与批次告警（B-06 + 4.4，tasks.md 4.1/4.4）

修复前：
  · 前端 uploadFolder(files, year) 收了年份却向后端传 undefined
    → 文件夹导入的批次 period_month 恒空，成本/汇率/广告全部静默按当前月归集
  · 合并工作簿告警只进运行日志（刷新即失），批次报告不可见

修复后：
  · 指定归属月份 → 批次 period_month 精确记录 + period_source='user_specified'
  · 未指定 → period_source='default_current'，批次报告带"按当前月归集"标注
  · 合并工作簿（文件名含 01-03月 / Q1 特征）→ 批次报告 warnings 出现
    kind='merged_workbook' 告警条目
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import UK_HEADER, build_xlsx, sql_one, upload_and_wait

# 冷门月份（2026-03/04）数据，避免污染 2026-01/2026-02 的既有断言月份
PT_ROW = ['2026-03-15 10:00:00 UTC', 'Order', '202-PT01', 'SKU-PT', 'B0PTTEST001',
          1, 30.00, 0, 0, -3.00, -2.00, 0, 25.00]


class TestPeriodPassthrough:
    def test_specified_period_recorded_on_batch(self, client, admin_headers, tmp_path):
        """指定归属月份 2026-03 → 批次 period_month=2026-03 + user_specified"""
        f = build_xlsx(tmp_path / 'LMG-PT_2026-03.xlsx', [
            ('UK销售', [UK_HEADER] + [PT_ROW]),
        ])
        st = upload_and_wait(client, admin_headers, f, f.name, 'LMG', '2026-03')
        assert st['status'] == 'success', st.get('error')
        b = sql_one("SELECT * FROM import_batch WHERE file_name=:n", n=f.name)
        assert b, '批次未落库'
        assert b['period_month'] == '2026-03'
        assert b['period_source'] == 'user_specified'
        rep = client.get(f"/api/v2/import/batches/{b['id']}/report",
                         headers=admin_headers).json()
        assert rep['period_month'] == '2026-03'
        assert rep['period_source'] == 'user_specified'

    def test_unspecified_period_marked_default_current(self, client, admin_headers, tmp_path):
        """未指定月份 → period_source='default_current'，报告可见回退标注（spec 5.2.1 R1）"""
        f = build_xlsx(tmp_path / 'LMG-PT-NOM_2026-04.xlsx', [
            ('UK销售', [UK_HEADER] + [PT_ROW]),
        ])
        st = upload_and_wait(client, admin_headers, f, f.name, 'LMG', None)
        assert st['status'] == 'success', st.get('error')
        b = sql_one("SELECT * FROM import_batch WHERE file_name=:n", n=f.name)
        assert b['period_month'] is None
        assert b['period_source'] == 'default_current'
        rep = client.get(f"/api/v2/import/batches/{b['id']}/report",
                         headers=admin_headers).json()
        assert rep['period_source'] == 'default_current'
        assert rep['period_source_note'] and '当前月' in rep['period_source_note']

    def test_merged_workbook_warning_in_batch_report(self, client, admin_headers, tmp_path):
        """合并工作簿（01-03月）→ 批次报告 warnings 含 merged_workbook 条目（4.4）"""
        f = build_xlsx(tmp_path / 'LMG_01-03月.xlsx', [
            ('UK广告', [['商品', '花费', '销售额'], ['B0PTTEST001', 10.0, 100.0]]),
        ])
        st = upload_and_wait(client, admin_headers, f, f.name, 'LMG', '2026-03')
        assert st['status'] == 'success', st.get('error')
        b = sql_one("SELECT * FROM import_batch WHERE file_name=:n", n=f.name)
        rep = client.get(f"/api/v2/import/batches/{b['id']}/report",
                         headers=admin_headers).json()
        warns = rep.get('warnings') or []
        mw = [w for w in warns if w.get('kind') == 'merged_workbook']
        assert mw, f'批次报告缺少合并工作簿告警: {warns}'
        assert '偏大' in mw[0]['message'] or '归入' in mw[0]['message']