# E2AG 在线投稿字段卡（2026-08-16）

本文件用于《软件学报》“人工智能操作系统及其安全”专刊在线投稿时复制字段。以投稿系统实际必填项为准；标记“暂缺”的内容不得自行推测。

## 投稿固定项

- 投稿类型：专刊投稿
- 专刊名称/备注：人工智能操作系统及其安全
- 稿件类别：建议选择“研究论文”（若系统提供该选项）
- 中图法分类号：TP309
- 基金项目：无
- 建议审稿人：无
- 回避审稿人：无
- 第一轮通过后的会议报告人：徐刚

## 中英文题名

中文题名：事件驱动智能体操作系统跨层能力治理方法（人工智能操作系统及其安全专刊）

专刊标注：已按投稿页面要求同步写入在线投稿字段、匿名稿首页和作者稿首页。

英文题名：A Cross-Layer Capability Governance Method for Event-Driven Agent Operating Systems

## 中文摘要与关键词

中文摘要：事件驱动智能体操作系统将外部事件转化为自主任务和工具副作用，但事件来源、任务创建与工具执行分属不同信任域，协议格式校验或单点动作过滤无法证明一次副作用获得了完整链路授权。本文提出事件到智能体治理（Event-to-Agent Governance，E2AG）方法，将事件声明权、任务创建权和工具副作用权组织为同一任务作用域能力生命周期。E2AG 通过来源–类型契约和目标策略完成任务准入，为获准任务签发对象绑定的短期能力，并在实际模型上下文协议（Model Context Protocol，MCP）调用抵达上游前实施第二次强制验证；统一执行证据用于检查授权依赖和定位治理失败。本文给出跨层授权状态模型、完整中介安全性质及其证明概要，并在事件驱动智能体操作系统原型上实现该方法。基于经盲表复核的冻结治理矩阵、合成压力集、持久化端到端执行、既有模型工具决策回放、自有设施治理一致性回归和并发故障注入进行评估。评估结果表明，在所测试场景中，完整方法保持了全部直接准入事件的可用性，并对冻结治理矩阵中全部非直接准入事件作出预期的拒绝或审批判定。消融实验显示，移除任一执行点均会重新产生与该点可见上下文相对应的未授权工具副作用。

中文关键词：人工智能操作系统；智能体操作系统；能力治理；完整中介；执行溯源

## English abstract and key words

Abstract: Event-driven agent operating systems transform external events into autonomous tasks and tool side effects. Event provenance, task creation, and tool execution nevertheless belong to distinct trust domains, so protocol validation or a single action filter cannot establish that a side effect is authorized by the complete execution chain. This paper presents Event-to-Agent Governance (E2AG), a cross-layer capability-governance method that organizes event-declaration, task-creation, and tool-side-effect authorities into a task-scoped capability lifecycle. E2AG admits tasks through source-type contracts and target policies, issues an object-bound short-lived capability for each admitted task, and enforces the capability again before an actual Model Context Protocol (MCP) call reaches its upstream tool. Unified execution evidence checks authorization dependencies and localizes governance failures. We define a cross-layer authorization transition model and a complete-mediation safety property with a proof sketch, and implement E2AG in an event-driven agent operating system prototype. Evaluation uses a blindly reviewed frozen governance matrix, a synthetic stress suite, persistent end-to-end executions, replay of previously frozen model tool decisions, a governance-consistency regression on self-operated facilities, and concurrent fault injection. The results show that, in the tested scenarios, the complete method preserves the availability of all directly admissible events and produces the expected denial or approval decision for every non-directly admissible event in the frozen governance matrix. The ablation study shows that removing either enforcement point reintroduces unauthorized tool side effects corresponding to the context visible at that point.

Key words: artificial intelligence operating system; agent operating system; capability governance; complete mediation; execution provenance

## 作者顺序与单位映射

> 2026-08-17 更新：九位作者顺序已确认；原5人投稿声明已失效，不能用于本次投稿。

1. 徐刚（Gang Xu），高级工程师，工学硕士，gxu@sei.ecnu.edu.cn，ORCID 0000-0001-8203-0307；国家可信嵌入式软件工程技术研究中心、华东师范大学软件工程学院。
2. 冯骐（Qi Feng），高级工程师，qfeng@admin.ecnu.edu.cn；华东师范大学信息化治理办公室。学位、ORCID 暂缺。
3. 姚俊杰（Junjie Yao），副教授，junjie.yao@sei.ecnu.edu.cn；华东师范大学软件工程学院。学位、ORCID 暂缺。
4. 肖宇（Yu Xiao），yxiao@sei.ecnu.edu.cn；华东师范大学软件工程学院。职称、学位、ORCID 暂缺。
5. 陈铭松（Mingsong Chen），mschen@sei.ecnu.edu.cn；华东师范大学软件工程学院。职称、学位、ORCID 暂缺。
6. 王江涛（Jiangtao Wang），教授级高级工程师，工学硕士，jtwang@sei.ecnu.edu.cn；国家可信嵌入式软件工程技术研究中心、华东师范大学软件工程学院。ORCID 暂缺。
7. 杨世光（Shiguang Yang），71265902107@stu.ecnu.edu.cn；华东师范大学软件工程学院。职称、学位、ORCID 暂缺。
8. 饶振宇（Zhenyu Rao），51285902217@stu.ecnu.edu.cn；华东师范大学软件工程学院。职称、学位、ORCID 暂缺。
9. 王语欣（Yuxin Wang），51285902161@stu.ecnu.edu.cn；华东师范大学软件工程学院。职称、学位、ORCID 暂缺。

通讯作者：冯骐，qfeng@admin.ecnu.edu.cn；手机号码暂缺，必须在提交前补齐。

## 单位信息

1. 国家可信嵌入式软件工程技术研究中心 / National Engineering Research Center for Trustworthy Embedded Software
   - 通信地址：上海市普陀区金沙江路1006号华东师大科技园D栋6层6A（国家工程中心临时办公室）
   - 邮编：暂缺
2. 华东师范大学信息化治理办公室 / Information Technology Services Center
   - 通信地址、邮编：暂缺
3. 华东师范大学软件工程学院 / Software Engineering Institute
   - 通信地址、邮编：暂缺

## 可选投稿说明

本文面向事件驱动智能体操作系统中的智能体协同安全与调度防护问题，研究外部事件、系统任务与工具调用跨越不同信任域时的授权传播。论文提出 E2AG 跨层能力治理方法，并通过完整消融、独立策略引擎基线、端到端执行和治理状态实验验证双执行点完整中介的作用。稿件拟投“人工智能操作系统及其安全”专刊。

## 附件核对

- 匿名论文：`E2AG-paper-anonymous-v1.1.29-20260817.pdf`
  - SHA-256：`2946D175678045645C7D7FE375A4304F5A53AA89B74BC6160889BAF2F8D36F7E`
- 投稿声明：使用作者自有模板；手写签名、填写日期和上传均由作者处理，仓库不生成声明文件。
- 保密审查单：作者已准备，由作者在投稿时上传；无需在本仓库继续处理。

## 提交前不得遗漏

1. 补通信作者手机号码；
2. 若投稿系统将作者单位、邮编、职称或邮箱设为必填，补齐上述“暂缺”字段；
3. 按已确认的九位作者顺序录入，并核对单位映射；
4. 使用九人新版投稿声明；保密审查单已由作者准备，全体手写签名和上传由作者处理；
5. 核对原创性及既往公开发表情况；若存在会议前作，补交全文和新增创新点说明；
6. 在最终确认页逐项检查题名及专刊标注、摘要、关键词、作者顺序、通讯作者、投稿类型和全部附件。
