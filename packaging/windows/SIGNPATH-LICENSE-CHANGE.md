# SignPath — 通知授權變更（Apache-2.0 → AGPL-3.0-or-later）

**狀態**：待寄出（2026-09-04）

## 為什麼要寄

2026-06-17 核准的 OSS subscription，申請表上填的授權是 **`Apache-2.0`**。
專案已於 **v1.14.48（2026-08-24）改為 `AGPL-3.0-or-later`**（`LICENSE` 全文
＋ `pyproject.toml` ＋ README 徽章 ＋ 介紹站 ＋ THIRD-PARTY-NOTICES）。

**主控台沒有授權欄位可以自己改** —— Organization 頁只有 Name /
Organization ID / Subscription type。授權寫在 Foundation 的申請審核記錄裡，
只能寫信請他們更新。

**不影響資格、不需重新核發憑證**：AGPL-3.0 是 OSI 核准授權，符合
SignPath Foundation 對 OSS subscription 的條件；憑證主體是組織名稱，
與授權宣告無關。CI（`release-windows-installer.yml`）不用動。

## 收件人

`support@signpath.io`（副本 Foundation 申請時往來的那條線）

## 信稿

> **Subject:** License change notification — jt-doc-tools [OSS] (Apache-2.0 → AGPL-3.0-or-later)
>
> Hello SignPath Foundation team,
>
> I would like to notify you of a license change for our OSS subscription.
>
> - Organization: jt-doc-tools [OSS]
> - Organization ID: 039e5b0f-76ba-4783-b526-390c46b8aef3
> - Repository: https://github.com/jasoncheng7115/jt-doc-tools
> - License at the time of application (approved 2026-06-17): Apache-2.0
> - Current license (since 2026-08-24, release v1.14.48): AGPL-3.0-or-later
>
> The project remains fully open source and publicly developed on GitHub;
> the change is from one OSI-approved license to another. Please let me know
> if this requires any action on my side, or if the subscription record can
> simply be updated.
>
> Thank you for supporting open source projects.
>
> Best regards,
> Jason Cheng

## 寄出後

在本檔頂端把狀態改成「已寄出 <日期>」，收到回覆再記一次結論；
`SIGNPATH-APPLICATION.md` 第 48 行與 116 行的 `Apache-2.0` 一併改掉。
