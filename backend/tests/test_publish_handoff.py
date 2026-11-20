"""发布交接链路的回归测试：审核 → 发布 → 采集批次 → 再提交。

用标准库 unittest，直接跑：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.extract_studio import ExtractStudioService
from app.services.handoff import COLLECTION_BATCHES, PUBLISH_CANDIDATES, REVIEW_RECEIPTS, SITE_DATASETS
from app.services.template_rule import TemplateRuleService
from app.store import store

SITE = "shop.example.com"
DETAIL_TASK = 1
LIST_TASK = 2


class PublishHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.studio = ExtractStudioService()
        self.templates = TemplateRuleService()

    def _publish_task(self, task_id: int) -> dict:
        self.studio.submit_review(task_id)
        return self.studio.publish(task_id)

    def _batch_for(self, template_id: int) -> dict:
        collection_input = self.templates.collection_input(template_id)
        self.assertTrue(collection_input["ok"], collection_input)
        created = self.templates.create_collection_batch(
            template_id,
            confirmed_fields=[pair["field"] for pair in collection_input["pairs"]],
        )
        self.assertTrue(created["ok"], created)
        return self.templates.run_collection_batch(created["batch"]["id"])

    # ------------------------------------------------------------ 主链路

    def test_two_layouts_publish_and_collect(self) -> None:
        detail = self.studio.submit_review(DETAIL_TASK)
        listing = self.studio.submit_review(LIST_TASK)
        self.assertEqual(detail["receipt"]["review_version"], "v1")
        self.assertEqual(listing["receipt"]["review_version"], "v1")
        self.assertEqual(len(store.rows(REVIEW_RECEIPTS)), 2)

        report = self.studio.conflict_report(DETAIL_TASK)
        self.assertFalse(report["blocking"])
        self.assertEqual(
            sorted(report["impacted_fields"]), ["价格", "商品标题"]
        )
        self.assertEqual(
            sorted(report["impacted_pages"]),
            [
                "https://shop.example.com/item/1001",
                "https://shop.example.com/list/phone",
            ],
        )
        for conflict in report["conflicts"]:
            self.assertEqual(conflict["severity"], "提示")
            self.assertEqual(len(conflict["variants"]), 2)

        detail_publish = self.studio.publish(DETAIL_TASK)
        list_publish = self.studio.publish(LIST_TASK)
        self.assertEqual(detail_publish["publish_status"], "已发布")
        self.assertEqual(list_publish["publish_status"], "已发布")
        self.assertEqual(len(detail_publish["template_ids"]), 1)
        self.assertEqual(len(list_publish["template_ids"]), 1)

        detail_template = store.find("template_rule", detail_publish["template_ids"][0])
        list_template = store.find("template_rule", list_publish["template_ids"][0])
        self.assertEqual(detail_template["审核版本"], "v1")
        self.assertEqual(list_template["审核版本"], "v1")
        self.assertIn("h1.product-title", detail_template["字段选择器"])
        self.assertIn(".item-card .title", list_template["字段选择器"])

        # 工坊清单回写发布结果，审核模块可以按任务同步采集版本。
        task = store.find("extract_studio", DETAIL_TASK)
        self.assertEqual(task["status"], "已发布")
        self.assertEqual(task["发布版本"], "v1")
        version = self.studio.collection_version(DETAIL_TASK)
        self.assertTrue(version["effective"])
        self.assertEqual(version["review_version"], "v1")

        # 两个布局各自的采集任务：字段数据与审核状态都能核对。
        detail_run = self._batch_for(detail_publish["template_ids"][0])
        list_run = self._batch_for(list_publish["template_ids"][0])
        self.assertTrue(detail_run["ok"])
        self.assertTrue(list_run["ok"])
        self.assertEqual(detail_run["batch"]["confirmed_fields"], ["商品标题", "价格", "库存"])
        self.assertEqual(list_run["batch"]["confirmed_fields"], ["商品标题", "价格"])
        self.assertIn("库存", detail_run["batch"]["field_data"])
        self.assertNotIn("库存", list_run["batch"]["field_data"])
        self.assertEqual(
            detail_run["batch"]["review_state_at_complete"]["task_status"], "已发布"
        )
        self.assertEqual(detail_run["batch"]["review_state_at_complete"]["review_version"], "v1")
        for records in list_run["batch"]["field_data"].values():
            self.assertTrue(all(record["matched"] for record in records))

        # 再次提交其中一份：同一审核版本不重复生效。
        resubmit = self.studio.submit_review(DETAIL_TASK)
        self.assertTrue(resubmit["duplicate"])
        self.assertEqual(resubmit["receipt"]["review_version"], "v1")
        self.assertEqual(len(store.rows(REVIEW_RECEIPTS)), 2)
        self.assertEqual(len(store.rows(PUBLISH_CANDIDATES)), 2)
        self.assertEqual(store.find("extract_studio", DETAIL_TASK)["status"], "已发布")

        # 重复发布同样只生效一次。
        republish = self.studio.publish(DETAIL_TASK)
        self.assertTrue(republish["duplicate"])
        self.assertEqual(len(store.rows(SITE_DATASETS)), 2)
        self.assertEqual(
            len([row for row in store.rows("template_rule") if row.get("来源任务") == DETAIL_TASK]),
            1,
        )

    # ------------------------------------------------------------ 冲突暂挂

    def test_same_layout_conflict_is_held_with_pages(self) -> None:
        self.studio.submit_review(5)
        self.studio.submit_review(6)
        first = self.studio.publish(5)
        second = self.studio.publish(6)

        for result in (first, second):
            self.assertTrue(result["held"])
            self.assertEqual(result["publish_status"], "暂挂")
            self.assertEqual(result["impacted_fields"], ["商品名称"])
            self.assertEqual(
                result["impacted_pages"],
                [
                    "https://mall.example.com/goods/7001",
                    "https://mall.example.com/goods/7002",
                ],
            )
            self.assertEqual(result["dataset"]["status"], "暂挂")
            self.assertEqual(result["template_ids"], [])

        conflict = self.studio.conflict_report(5)["conflicts"][0]
        self.assertEqual(conflict["severity"], "阻断")
        self.assertEqual(conflict["layouts"], ["商品详情"])
        self.assertIn("https://mall.example.com/goods/7001", conflict["explanation"])

        # 暂挂的任务仍是有效审核版本，但不会生成模板，也不会交付采集输入。
        task = store.find("extract_studio", 5)
        self.assertEqual(task["status"], "已审核")
        self.assertEqual(task["发布状态"], "暂挂")
        self.assertFalse(self.studio.collection_version(5)["effective"])
        self.assertEqual(len(store.rows(COLLECTION_BATCHES)), 0)

        again = self.studio.publish(5)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["publish_status"], "暂挂")
        self.assertEqual(len(store.rows(SITE_DATASETS)), 2)

    def test_held_publish_resumes_after_conflict_resolved(self) -> None:
        self.studio.submit_review(5)
        self.studio.submit_review(6)
        self.studio.publish(5)
        self.studio.publish(6)
        self.assertEqual(len(store.rows(SITE_DATASETS)), 2)

        # 消除冲突：任务 6 的标注被修正后重新提交为新审核版本；随后任务 5 再发布。
        task6 = store.find("extract_studio", 6)
        task6["字段配对"] = "商品名称=.goods-name"
        self.studio.submit_review(6)
        self.studio.publish(6)
        resumed = self.studio.publish(5)
        self.assertEqual(resumed["publish_status"], "已发布")
        self.assertFalse(resumed["held"])
        self.assertEqual(len(resumed["template_ids"]), 1)
        self.assertEqual(store.find("extract_studio", 5)["status"], "已发布")
        self.assertEqual(len(store.rows(SITE_DATASETS)), 3)

    def test_held_dataset_rejects_collection_input(self) -> None:
        self.studio.submit_review(5)
        self.studio.publish(5)
        dataset = store.rows(SITE_DATASETS)[0]
        task = store.find("extract_studio", 5)
        receipt = self.studio.current_receipt(5)
        template = {
            "id": 90,
            "status": "已发布",
            "模板名称": "held-template",
            "适用站点": dataset["site"],
            "提取字段": "商品名称",
            "字段选择器": ".goods-name",
            "字段配对": "商品名称=.goods-name",
            "来源数据集": dataset["id"],
            "审核版本": receipt["review_version"],
            "来源任务": task["id"],
        }
        store.rows("template_rule").append(template)
        result = self.templates.collection_input(90)
        self.assertFalse(result["ok"])
        self.assertIn("暂挂", result["reason"])

    # ------------------------------------------------------------ 完整字段配对

    def test_batch_requires_complete_field_pairs(self) -> None:
        publish = self._publish_task(DETAIL_TASK)
        template_id = publish["template_ids"][0]

        partial = self.templates.create_collection_batch(
            template_id, confirmed_fields=["商品标题", "价格"]
        )
        self.assertFalse(partial["ok"])
        self.assertIn("库存", partial["reason"])

        extra = self.templates.create_collection_batch(
            template_id, confirmed_fields=["商品标题", "价格", "库存", "评分"]
        )
        self.assertFalse(extra["ok"])
        self.assertIn("评分", extra["reason"])
        self.assertEqual(len(store.rows(COLLECTION_BATCHES)), 0)

        complete = self.templates.create_collection_batch(
            template_id, confirmed_fields=["商品标题", "价格", "库存"]
        )
        self.assertTrue(complete["ok"])
        self.assertEqual(len(complete["batch"]["selector_snapshot"]), 3)

    def test_template_without_dataset_cannot_feed_collection(self) -> None:
        store.rows("template_rule").append({
            "id": 77,
            "status": "已发布",
            "模板名称": "手工模板",
            "适用站点": SITE,
            "提取字段": "标题",
            "字段选择器": "h1",
            "字段配对": "标题=h1",
        })
        result = self.templates.collection_input(77)
        self.assertFalse(result["ok"])

    # ------------------------------------------------------------ 版本追溯

    def test_new_version_keeps_old_batch_traceable(self) -> None:
        publish = self._publish_task(DETAIL_TASK)
        template_id = publish["template_ids"][0]
        old_run = self._batch_for(template_id)
        old_batch = old_run["batch"]
        old_selector = next(
            item["selector"] for item in old_batch["selector_snapshot"] if item["field"] == "价格"
        )
        self.assertEqual(old_selector, ".price-current")

        # 修改标注后再次提交：产生 v2，旧候选被取代。
        task = store.find("extract_studio", DETAIL_TASK)
        task["字段配对"] = "商品标题=h1.product-title, 价格=.price-sale, 库存=#stock-count"
        submitted = self.studio.submit_review(DETAIL_TASK)
        self.assertFalse(submitted["duplicate"])
        self.assertEqual(submitted["receipt"]["review_version"], "v2")
        self.assertEqual(task["审核版本"], "v2")
        self.assertEqual(submitted["candidate"]["status"], "待发布")

        # 已完成的批次仍按创建时的选择器快照追溯，不受候选状态变化影响。
        stored_batch = store.find(COLLECTION_BATCHES, old_batch["id"])
        self.assertEqual(stored_batch["selector_snapshot_hash"], old_batch["selector_snapshot_hash"])
        traced = self.templates.batch_detail(stored_batch)
        self.assertIn(
            {"field": "价格", "selector": ".price-current", "layout": "商品详情",
             "pages": ["https://shop.example.com/item/1001"]},
            traced["selector_snapshot"],
        )
        self.assertEqual(traced["review_version"], "v1")
        self.assertEqual(traced["field_data"]["价格"][0]["selector"], ".price-current")
        self.assertEqual(
            self.studio.trace(DETAIL_TASK)["batches"][0]["selector_snapshot_hash"],
            old_batch["selector_snapshot_hash"],
        )

        # 发布 v2 之后，旧模板与旧数据集失效，候选仍能追到各自回执。
        published_v2 = self.studio.publish(DETAIL_TASK)
        self.assertEqual(published_v2["publish_status"], "已发布")
        self.assertNotEqual(published_v2["template_ids"][0], template_id)
        self.assertEqual(store.find("template_rule", template_id)["status"], "已失效")
        candidates = [
            item for item in self.studio.list_candidates() if item["task_id"] == DETAIL_TASK
        ]
        self.assertEqual([item["review_version"] for item in candidates], ["v1", "v2"])
        self.assertEqual([item["status"] for item in candidates], ["已失效", "已发布"])
        self.assertTrue(all(item["traceable"] for item in candidates))
        self.assertEqual(
            {item["receipt"]["review_version"] for item in candidates}, {"v1", "v2"}
        )

        new_input = self.templates.collection_input(published_v2["template_ids"][0])
        self.assertIn({"field": "价格", "selector": ".price-sale"}, new_input["pairs"])

    def test_review_rejects_incomplete_field_pairs(self) -> None:
        task = store.find("extract_studio", DETAIL_TASK)
        original = task["字段配对"]

        task["字段配对"] = "商品标题=h1.product-title, 价格=, 库存=#stock-count"
        self.assertIn("空选择器", self.studio.submit_review(DETAIL_TASK)["reason"])

        task["字段配对"] = "商品标题=h1.product-title, 价格=.price-current"
        self.assertIn("缺少字段：库存", self.studio.submit_review(DETAIL_TASK)["reason"])
        self.assertEqual(store.rows(REVIEW_RECEIPTS), [])

        task["字段配对"] = original
        self.assertTrue(self.studio.submit_review(DETAIL_TASK)["ok"])

    def test_action_route_publish_is_idempotent(self) -> None:
        _, message = self.studio.run_action(DETAIL_TASK, "发布规则")
        self.assertIn("已发布", message)
        self.assertEqual(store.find("extract_studio", DETAIL_TASK)["status"], "已发布")

        _, again = self.studio.run_action(DETAIL_TASK, "发布规则")
        self.assertIn("不重复生效", again)
        self.assertEqual(len(store.rows(REVIEW_RECEIPTS)), 1)
        self.assertEqual(len(store.rows(PUBLISH_CANDIDATES)), 1)
        self.assertEqual(len(store.rows(SITE_DATASETS)), 1)


if __name__ == "__main__":
    unittest.main()
