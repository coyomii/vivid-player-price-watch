# Vivid Player 打折提醒

监控 [Vivid Player 中国区商店价格](https://apps.microsoft.com/detail/9pf8wt09d2nt?hl=zh-CN&gl=CN)，通过 GitHub Issue 提醒。

- 北京时间每天 **08:00、20:00** 检查两次。GitHub Actions 明确配置 `timezone: Asia/Shanghai` 和 `0 8,20 * * *`；平台可能延迟触发，严重时还可能丢弃任务，因此不能保证准点运行。
- 初始基准价格 **CNY 25.00**。正式版现价低于商店标价或基准价时提醒；与上次检查相比直接降价也提醒。
- 开始促销、同一促销再创新低、恢复原价后再次促销，均会提醒。同一价格不会每天重复提醒。
- 价格无变化时只更新记录，不创建 Issue。接口连续三次连接失败、失去有效报价等情况创建一条异常 Issue；恢复后自动关闭。
- 仅选取正式版 SKU `0010`、中国区、人民币、Windows 桌面端、处于有效期内的 `Purchase` 报价。不会把免费试用、其他地区或纯许可记录误判为限免。
- 纯 Python 标准库；使用 GitHub CLI `gh` 操作仓库。GitHub 托管 Ubuntu runner 已带 `gh`，不需要邮箱、SMTP 密码或个人访问令牌。

## 使用

1. 将整个项目（包括 `.github` 隐藏目录）上传到自己的私有 GitHub 仓库，默认分支设为 `main`，保持 Issues 启用。
2. 把 `config.json` 的 `notify_user` 改成自己的 GitHub 登录名。这个用户需要能访问仓库；建议设为仓库所有者。
3. 打开 **Actions → Vivid Player 折扣提醒 → Run workflow**，第一次可以勾选 `test_notification` 验证通知。
4. 任务会自动创建 `watch-state` 分支，把最后价格、提醒状态和最近 180 次报价写入 `state.json`。不要删除这个分支，否则去重记录也会丢失。

仓库工作流只使用本次运行自动生成的 `GITHUB_TOKEN`，权限限于此仓库的内容读写和 Issues 读写。

## 通知怎么收到

新折扣会创建一条 Issue，正文 `@提及` 并指派给 `notify_user`。可在 GitHub 站内通知或 GitHub 手机 App 查看。

若希望同时收邮件，在 [GitHub 通知设置](https://github.com/settings/notifications) 中为参与、提及及指派的通知启用 **Email**，并确认接收邮箱。GitHub 的邮件设置和过滤规则会影响送达；本工具不直接发邮件。

第一次生成的“通知测试”Issue 明确标注为测试，不表示商品正在促销。可以自行关闭，不影响后续监控。

## 本地检查

Python 3.12+：

```sh
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

`--dry-run` 只查询真实价格，不读写 GitHub，不发送任何通知。

本地实际运行还需要先 `gh auth login`，然后：

```sh
python monitor.py --repo YOUR_NAME/YOUR_REPO
python monitor.py --repo YOUR_NAME/YOUR_REPO --test-notification
```

## 修改监控规则

直接编辑 `config.json`。当前配置是 Vivid Player 正式版、中国区、人民币，基准价 25 元。`baseline_price` 不是购买目标价，而是用于识别下调后的价格；如商店永久调价，可自行修改基准。

修改 `product_id`、`sku_id`、`market` 或 `currency` 时，应使用新的 `state_branch`，以避免混用旧产品或旧币种的记录。若要监控其他商品，需确认其正式版 SKU 和当地币种。

## 数据来源与限制

价格来自微软公开的 `displaycatalog.mp.microsoft.com/v7.0/products` 接口，无需登录。它不是为本项目承诺稳定性的 API，字段变化会使监控明确失败，而不会把缺失数据写为零元。

公开报价可能与账号专属优惠、结算价格或应用内购不同；本工具只监控页面对应正式版的购买报价。Actions 中记录的报价有效期也不保证价格期间不变，实际价格以结算页面为准。

GitHub Actions 免费运行额度受账户套餐影响。关闭电脑不影响云端定时运行；暂停、删除工作流或仓库则会停止检查。
