# 增加海上工程变更影响审批基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配和调度情景；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `src/change_control/`：资源台账、设施快照、设计修订变更申请、影响核算、整体审批占定和现场回执；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
PYTHONPATH=src python3 -m change_control.acceptance --workspace .
```

四条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析、并网审批和工程变更影响审批，不访问外部网络。

## 工程变更流程

`change_control` 模块把设计修订纳入正式变更流程：

1. 计划人员维护资源台账（集电线路、升压站接入、施工船窗口、应急救援覆盖四类约束池）并可随时登记设施快照；
2. 工程人员提交带版本的设备清单、坐标、预计投运曲线、保障等级和接入需求，系统自动取当时的设施快照，计算每项约束的剩余量以及与其他申请的冲突并持久化核算结果；
3. 施工时段、资源预留和撤回方案作为整体由非申请人审批，批准事务内复核实时余量后占定；
4. 现场步骤的回执推动变更前进，局部失败只能进入明确回退或人工接管，不会被汇总成成功；
5. 后续修订通过 `supersedes_change_id` 关联原申请并释放其占定，历史申请、回执和决定全部保留；
6. 查询端 `GET /changes/{id}/explain` 还原每次决定依赖的快照、约束余量和冲突对象，`GET /changes/{id}/lineage` 查看修订链。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m change_control.api --database change.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
