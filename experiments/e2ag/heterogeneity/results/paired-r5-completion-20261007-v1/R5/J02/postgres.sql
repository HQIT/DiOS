--
-- PostgreSQL database dump
--

\restrict HPny4PnLb75qs5I6fiksc81Hzbxzhpy1IpTLGI0qDaE34MZlh0KefuLwXU8IXk8

-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: a2a_tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.a2a_tasks (
    id character varying(32) NOT NULL,
    agent_id character varying(12) NOT NULL,
    context_id character varying(64) NOT NULL,
    trace_id character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    message json NOT NULL,
    artifacts json NOT NULL,
    error text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: agent_runtimes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runtimes (
    agent_id character varying(12) NOT NULL,
    container_id character varying(64) NOT NULL,
    url character varying(256) NOT NULL,
    status character varying(16) NOT NULL,
    started_at timestamp with time zone NOT NULL
);


--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    id character varying(12) NOT NULL,
    name character varying(128) NOT NULL,
    mode character varying(16) NOT NULL,
    "group" character varying(128) NOT NULL,
    role character varying(16) NOT NULL,
    description text NOT NULL,
    model character varying(128) NOT NULL,
    system_prompt text NOT NULL,
    skills json NOT NULL,
    mcp_config_path character varying(512) NOT NULL,
    mcp_server_ids json NOT NULL,
    workspace_path character varying(512) NOT NULL,
    capabilities json NOT NULL,
    env json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_messages (
    id character varying(12) NOT NULL,
    session_id character varying(36) NOT NULL,
    role character varying(16) NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_sessions (
    id character varying(36) NOT NULL,
    agent_id character varying(12) NOT NULL,
    title character varying(256) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connectors (
    id character varying(12) NOT NULL,
    type character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    enabled boolean NOT NULL,
    config json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: e2ag_approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.e2ag_approvals (
    id character varying(32) NOT NULL,
    event_log_id character varying(12) NOT NULL,
    trace_id character varying(32) NOT NULL,
    status character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone,
    actor character varying(128) NOT NULL,
    reason text NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: e2ag_tool_grants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.e2ag_tool_grants (
    id character varying(32) NOT NULL,
    token_hash character varying(64) NOT NULL,
    trace_id character varying(32) NOT NULL,
    task_id character varying(32) NOT NULL,
    event_log_id character varying(12) NOT NULL,
    agent_id character varying(12) NOT NULL,
    mcp_server_id character varying(12) NOT NULL,
    allowed_tools json NOT NULL,
    argument_constraints json,
    status character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    call_count integer NOT NULL,
    last_used_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: e2ag_tool_invocations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.e2ag_tool_invocations (
    id character varying(32) NOT NULL,
    grant_id character varying(32) NOT NULL,
    event_log_id character varying(12) NOT NULL,
    trace_id character varying(32) NOT NULL,
    task_id character varying(32) NOT NULL,
    method character varying(64) NOT NULL,
    tool_name text NOT NULL,
    request_sha256 character varying(64) NOT NULL,
    state character varying(32) NOT NULL,
    upstream_status integer,
    response_sha256 character varying(64),
    created_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone
);


--
-- Name: event_dedup_claims; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_dedup_claims (
    dedup_hash character varying(64) NOT NULL,
    owner_token character varying(32) NOT NULL,
    event_log_id character varying(12) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: event_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_logs (
    id character varying(12) NOT NULL,
    source character varying(256) NOT NULL,
    event_type character varying(128) NOT NULL,
    subject character varying(256) NOT NULL,
    cloud_event json NOT NULL,
    matched_agent_ids json NOT NULL,
    status character varying(32) NOT NULL,
    trace_id character varying(32) NOT NULL,
    contract_decision json NOT NULL,
    policy_decision json NOT NULL,
    audit_chain jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    retry_count integer NOT NULL,
    max_retries integer NOT NULL,
    next_retry_at timestamp with time zone,
    error_message text NOT NULL,
    dedup_hash character varying(64) NOT NULL
);


--
-- Name: hetero_conflicts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hetero_conflicts (
    key character varying(64) NOT NULL,
    identity character varying(64) NOT NULL,
    expected_digest character varying(64) NOT NULL,
    observed_digest character varying(64) NOT NULL
);


--
-- Name: hetero_receipts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hetero_receipts (
    identity character varying(64) NOT NULL,
    source character varying(256) NOT NULL,
    event_id character varying(128) NOT NULL,
    digest character varying(64) NOT NULL,
    phase character varying(32) NOT NULL,
    event_log_id character varying(12) NOT NULL
);


--
-- Name: mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_servers (
    id character varying(12) NOT NULL,
    name character varying(128) NOT NULL,
    transport character varying(32) NOT NULL,
    url character varying(512) NOT NULL,
    headers json NOT NULL,
    command character varying(512) NOT NULL,
    args json NOT NULL,
    env json NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.models (
    id character varying(12) NOT NULL,
    name character varying(128) NOT NULL,
    provider character varying(32) NOT NULL,
    model character varying(128) NOT NULL,
    base_url character varying(512) NOT NULL,
    api_key character varying(512) NOT NULL,
    display_name character varying(128) NOT NULL,
    description text NOT NULL,
    context_length integer,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skills (
    id character varying(12) NOT NULL,
    name character varying(128) NOT NULL,
    description text NOT NULL,
    source_url character varying(512) NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id character varying(12) NOT NULL,
    agent_id character varying(12) NOT NULL,
    source_pattern character varying(256) NOT NULL,
    event_types json NOT NULL,
    filter_rules json NOT NULL,
    cron_expression character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp with time zone NOT NULL
);


--
-- Data for Name: a2a_tasks; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.a2a_tasks (id, agent_id, context_id, trace_id, status, message, artifacts, error, created_at, updated_at) FROM stdin;
65d051a3584c4a7cba255751ca1e97a4	file-agent	f151a52f279c	20ffd44ced4648cfbbed2f8111c25099	failed	{"role": "user", "messageId": "256cb9c72c9141be8b5951b1df65015d", "parts": [{"kind": "text", "text": "[Event type=amqp.received source=amqp/fixture subject=delivery-1]"}, {"kind": "data", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}}], "_source": {"kind": "cloudevent", "id": "delivery-1", "type": "amqp.received", "source": "amqp/fixture"}, "_e2ag": {"trace_id": "20ffd44ced4648cfbbed2f8111c25099"}}	[{"qualification_call_status": 403, "response": {"jsonrpc": "2.0", "id": 2, "error": {"code": -32001, "message": "E2AG tool grant rejected", "data": {"reason": "TOOL_GRANT_NOT_ACTIVE"}}}}]		2026-10-07 08:17:26.899846+00	2026-10-07 08:17:26.899846+00
\.


--
-- Data for Name: agent_runtimes; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.agent_runtimes (agent_id, container_id, url, status, started_at) FROM stdin;
\.


--
-- Data for Name: agents; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.agents (id, name, mode, "group", role, description, model, system_prompt, skills, mcp_config_path, mcp_server_ids, workspace_path, capabilities, env, created_at) FROM stdin;
file-agent	Fixed file workflow	task		agent				[]		[]		{"governance": {"allowed_event_sources": ["amqp/fixture"], "allowed_event_types": ["amqp.received"], "allowed_actions": ["report.create", "secret.read"], "require_action_declaration": true, "allowed_tools": ["report.write"], "argument_constraints": {"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}}}	{}	2026-10-07 08:17:24.38105+00
\.


--
-- Data for Name: chat_messages; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.chat_messages (id, session_id, role, content, created_at) FROM stdin;
\.


--
-- Data for Name: chat_sessions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.chat_sessions (id, agent_id, title, created_at, updated_at) FROM stdin;
\.


--
-- Data for Name: connectors; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.connectors (id, type, name, enabled, config, created_at) FROM stdin;
\.


--
-- Data for Name: e2ag_approvals; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_approvals (id, event_log_id, trace_id, status, expires_at, decided_at, actor, reason, created_at) FROM stdin;
\.


--
-- Data for Name: e2ag_tool_grants; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_grants (id, token_hash, trace_id, task_id, event_log_id, agent_id, mcp_server_id, allowed_tools, argument_constraints, status, expires_at, call_count, last_used_at, created_at) FROM stdin;
ab58c41916664fb5824972f86f4767e4	58e4c4819c45bca853a90bcf9d8a43e20ad75cbda8f839b424d674cbcde4dc40	20ffd44ced4648cfbbed2f8111c25099	65d051a3584c4a7cba255751ca1e97a4	f151a52f279c	file-agent	file-tool	["report.write"]	{"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}	revoked	2026-10-07 08:32:26.924622+00	1	2026-10-07 08:17:27.190296+00	2026-10-07 08:17:26.92563+00
\.


--
-- Data for Name: e2ag_tool_invocations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_invocations (id, grant_id, event_log_id, trace_id, task_id, method, tool_name, request_sha256, state, upstream_status, response_sha256, created_at, finished_at) FROM stdin;
85d211108ffb4b5d8256160a69ece035	ab58c41916664fb5824972f86f4767e4	f151a52f279c	20ffd44ced4648cfbbed2f8111c25099	65d051a3584c4a7cba255751ca1e97a4	initialize		cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130	response_received	200	29be6f4a4b2718b68520477c42bd284301b18511b2d48e3ea09f34fe807fea31	2026-10-07 08:17:26.955399+00	2026-10-07 08:17:27.095643+00
e972a8427cf04ad9ba54f1298990bb4a	ab58c41916664fb5824972f86f4767e4	f151a52f279c	20ffd44ced4648cfbbed2f8111c25099	65d051a3584c4a7cba255751ca1e97a4	notifications/initialized		59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e	response_received	202	e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855	2026-10-07 08:17:27.119015+00	2026-10-07 08:17:27.131356+00
\.


--
-- Data for Name: event_dedup_claims; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_dedup_claims (dedup_hash, owner_token, event_log_id, expires_at, created_at) FROM stdin;
126983d6acb855e0dd5ba224e6a6ba3171242487ff874132d07a924aaaa84d0f	c12bdc02a7794163a0cf5b5de92c358e	f151a52f279c	2026-10-07 09:17:26.865709+00	2026-10-07 08:17:26.86671+00
\.


--
-- Data for Name: event_logs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_logs (id, source, event_type, subject, cloud_event, matched_agent_ids, status, trace_id, contract_decision, policy_decision, audit_chain, created_at, retry_count, max_retries, next_retry_at, error_message, dedup_hash) FROM stdin;
f151a52f279c	amqp/fixture	amqp.received	delivery-1	{"specversion": "1.0", "id": "delivery-1", "source": "amqp/fixture", "type": "amqp.received", "subject": "delivery-1", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}, "deliverysubject": "Qualification task", "deliverydigest": "6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484"}	["file-agent"]	failed	20ffd44ced4648cfbbed2f8111c25099	{"mode": "enforce", "stage": "contract", "decision": "allow", "reason_codes": ["CONTRACT_BOUND"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "amqp-qualification", "latency_us": 106, "evidence": {"source_pattern": "amqp/fixture", "event_type": "amqp.received"}}	{"mode": "enforce", "stage": "policy", "decision": "allow", "reason_codes": ["POLICY_ALLOWED"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "amqp-qualification", "latency_us": 69, "evidence": {"action": "report.create", "tool": "", "resource": "", "environment": "production"}, "effective_decision": "allow"}	[{"stage": "contract", "outcome": "allow", "evidence": {"stage": "contract", "decision": "allow", "evidence": {"event_type": "amqp.received", "source_pattern": "amqp/fixture"}, "latency_us": 106, "reason_codes": ["CONTRACT_BOUND"], "contract_type": "amqp-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 0, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "2f977097d2ce4b9ef77182984bf6da76ba07e013417fa8f9ec2e286e0e58d31c", "previous_hash": ""}, {"stage": "policy", "outcome": "allow", "evidence": {"stage": "policy", "decision": "allow", "evidence": {"tool": "", "action": "report.create", "resource": "", "environment": "production"}, "latency_us": 69, "reason_codes": ["POLICY_ALLOWED"], "contract_type": "amqp-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 1, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "5c45de4d2624ae14acdd789afaec07987279a6d5fc8484360554f613d54092d9", "previous_hash": "2f977097d2ce4b9ef77182984bf6da76ba07e013417fa8f9ec2e286e0e58d31c"}, {"stage": "tool_grant", "outcome": "grant_issued", "evidence": {"task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent", "grant_id": "ab58c41916664fb5824972f86f4767e4", "allowed_tools": ["report.write"], "mcp_server_id": "file-tool", "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 2, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "49e067ae07fdc6dcd2ec0fbad6f2db8143eec367b4df76e5a865e394148bc1b5", "previous_hash": "5c45de4d2624ae14acdd789afaec07987279a6d5fc8484360554f613d54092d9"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "initialize", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "grant_id": "ab58c41916664fb5824972f86f4767e4", "invocation_id": "85d211108ffb4b5d8256160a69ece035", "request_sha256": "cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130"}, "sequence": 3, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "b355dd78bcf62ddcae57251977d35a865f2b28ca83f61403465c71e251225bff", "previous_hash": "49e067ae07fdc6dcd2ec0fbad6f2db8143eec367b4df76e5a865e394148bc1b5"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "initialize", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent", "grant_id": "ab58c41916664fb5824972f86f4767e4", "invocation_id": "85d211108ffb4b5d8256160a69ece035", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 4, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "4b3b16fe59dea4e18b140d4aac5c77b29c198b1ef112aab14e34666b28208b3f", "previous_hash": "b355dd78bcf62ddcae57251977d35a865f2b28ca83f61403465c71e251225bff"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "notifications/initialized", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "grant_id": "ab58c41916664fb5824972f86f4767e4", "invocation_id": "e972a8427cf04ad9ba54f1298990bb4a", "request_sha256": "59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e"}, "sequence": 5, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "960c80cc8342239b0821485c01be197a07c571c2c565d9dc78a1482e0c354b42", "previous_hash": "4b3b16fe59dea4e18b140d4aac5c77b29c198b1ef112aab14e34666b28208b3f"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "notifications/initialized", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent", "grant_id": "ab58c41916664fb5824972f86f4767e4", "invocation_id": "e972a8427cf04ad9ba54f1298990bb4a", "mcp_server_id": "file-tool", "upstream_status": 202, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 6, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "b0acdec34aa9108d54e2f0242d7a2637670c5f038293641c222a71885e94c700", "previous_hash": "960c80cc8342239b0821485c01be197a07c571c2c565d9dc78a1482e0c354b42"}, {"stage": "tool_grant", "outcome": "grant_revoked", "evidence": {"reason": "task_lifecycle", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "grant_id": "ab58c41916664fb5824972f86f4767e4"}, "sequence": 7, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "3cfd7f5105ade419015029fda469832e6fab20467195dc161a9b0242b4ae9d5a", "previous_hash": "b0acdec34aa9108d54e2f0242d7a2637670c5f038293641c222a71885e94c700"}, {"stage": "tool_call", "outcome": "deny", "evidence": {"tool": "", "method": "initialize", "reason": "TOOL_GRANT_NOT_ACTIVE", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent", "grant_id": "ab58c41916664fb5824972f86f4767e4", "mcp_server_id": "file-tool", "upstream_status": null, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 8, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "4fddf116ba465ae7e99bc53275ae774ed7bbe408c10647a1d519b7079aee59a8", "previous_hash": "3cfd7f5105ade419015029fda469832e6fab20467195dc161a9b0242b4ae9d5a"}, {"stage": "tool_call", "outcome": "deny", "evidence": {"tool": "report.write", "method": "tools/call", "reason": "TOOL_GRANT_NOT_ACTIVE", "task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent", "grant_id": "ab58c41916664fb5824972f86f4767e4", "mcp_server_id": "file-tool", "upstream_status": null, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 9, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "b3892b2d08e2b4c0ec39d1df37f8b0f430b4f6e604b33ad482de1706fb0bffdd", "previous_hash": "4fddf116ba465ae7e99bc53275ae774ed7bbe408c10647a1d519b7079aee59a8"}, {"stage": "a2a_task", "outcome": "failed", "evidence": {"task_id": "65d051a3584c4a7cba255751ca1e97a4", "agent_id": "file-agent"}, "sequence": 10, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "84154f2cf9cbc95a8c609229ec6f1fb95211d0f404dfeb333ffaca1afa5ee128", "previous_hash": "b3892b2d08e2b4c0ec39d1df37f8b0f430b4f6e604b33ad482de1706fb0bffdd"}, {"stage": "dispatch", "outcome": "failed", "evidence": {"error": "Agent file-agent: ", "matched_agent_ids": ["file-agent"]}, "sequence": 11, "trace_id": "20ffd44ced4648cfbbed2f8111c25099", "entry_hash": "a5805c61194172b2a695b82350a655785481d03cf517c3959b030dc1aad4bea2", "previous_hash": "84154f2cf9cbc95a8c609229ec6f1fb95211d0f404dfeb333ffaca1afa5ee128"}]	2026-10-07 08:17:26.870709+00	0	3	2026-10-07 08:18:26.869711+00	Agent file-agent: 	126983d6acb855e0dd5ba224e6a6ba3171242487ff874132d07a924aaaa84d0f
\.


--
-- Data for Name: hetero_conflicts; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.hetero_conflicts (key, identity, expected_digest, observed_digest) FROM stdin;
\.


--
-- Data for Name: hetero_receipts; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.hetero_receipts (identity, source, event_id, digest, phase, event_log_id) FROM stdin;
5d51d000ea7e5a1798988b831dda7e022431adb72024eedc5b15601e0a0dc046	amqp/fixture	delivery-1	6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484	recorded	f151a52f279c
\.


--
-- Data for Name: mcp_servers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.mcp_servers (id, name, transport, url, headers, command, args, env, created_at) FROM stdin;
file-tool	receipt	stdio		{}	C:\\Users\\sugar\\Documents\\ChatGPT\\DiOS\\HQIT-DiOS-e2ag\\backend\\.venv\\Scripts\\python.exe	["C:\\\\Users\\\\sugar\\\\Documents\\\\ChatGPT\\\\DiOS\\\\HQIT-DiOS-e2ag\\\\experiments\\\\e2ag\\\\heterogeneity\\\\stdio_receipt_tool.py", "--directory", "C:\\\\Users\\\\sugar\\\\Documents\\\\ChatGPT\\\\DiOS\\\\HQIT-DiOS-e2ag\\\\experiments\\\\e2ag\\\\heterogeneity\\\\results\\\\paired-r5-completion-20261007-v1\\\\R5\\\\J02"]	{}	2026-10-07 08:17:24.389599+00
\.


--
-- Data for Name: models; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.models (id, name, provider, model, base_url, api_key, display_name, description, context_length, created_at) FROM stdin;
\.


--
-- Data for Name: skills; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.skills (id, name, description, source_url, content, created_at) FROM stdin;
\.


--
-- Data for Name: subscriptions; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.subscriptions (id, agent_id, source_pattern, event_types, filter_rules, cron_expression, enabled, created_at) FROM stdin;
b2334ad7b8a4	file-agent	amqp/*	["amqp.received"]	{}		t	2026-10-07 08:17:24.397094+00
\.


--
-- Name: a2a_tasks a2a_tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.a2a_tasks
    ADD CONSTRAINT a2a_tasks_pkey PRIMARY KEY (id);


--
-- Name: agent_runtimes agent_runtimes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runtimes
    ADD CONSTRAINT agent_runtimes_pkey PRIMARY KEY (agent_id);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT chat_messages_pkey PRIMARY KEY (id);


--
-- Name: chat_sessions chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_sessions
    ADD CONSTRAINT chat_sessions_pkey PRIMARY KEY (id);


--
-- Name: connectors connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connectors
    ADD CONSTRAINT connectors_pkey PRIMARY KEY (id);


--
-- Name: e2ag_approvals e2ag_approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.e2ag_approvals
    ADD CONSTRAINT e2ag_approvals_pkey PRIMARY KEY (id);


--
-- Name: e2ag_tool_grants e2ag_tool_grants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.e2ag_tool_grants
    ADD CONSTRAINT e2ag_tool_grants_pkey PRIMARY KEY (id);


--
-- Name: e2ag_tool_invocations e2ag_tool_invocations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.e2ag_tool_invocations
    ADD CONSTRAINT e2ag_tool_invocations_pkey PRIMARY KEY (id);


--
-- Name: event_dedup_claims event_dedup_claims_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dedup_claims
    ADD CONSTRAINT event_dedup_claims_pkey PRIMARY KEY (dedup_hash);


--
-- Name: event_logs event_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_logs
    ADD CONSTRAINT event_logs_pkey PRIMARY KEY (id);


--
-- Name: hetero_conflicts hetero_conflicts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hetero_conflicts
    ADD CONSTRAINT hetero_conflicts_pkey PRIMARY KEY (key);


--
-- Name: hetero_receipts hetero_receipts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hetero_receipts
    ADD CONSTRAINT hetero_receipts_pkey PRIMARY KEY (identity);


--
-- Name: mcp_servers mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);


--
-- Name: models models_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_name_key UNIQUE (name);


--
-- Name: models models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.models
    ADD CONSTRAINT models_pkey PRIMARY KEY (id);


--
-- Name: skills skills_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_name_key UNIQUE (name);


--
-- Name: skills skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: ix_a2a_tasks_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_agent_id ON public.a2a_tasks USING btree (agent_id);


--
-- Name: ix_a2a_tasks_context_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_context_id ON public.a2a_tasks USING btree (context_id);


--
-- Name: ix_a2a_tasks_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_a2a_tasks_trace_id ON public.a2a_tasks USING btree (trace_id);


--
-- Name: ix_chat_messages_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_messages_session_id ON public.chat_messages USING btree (session_id);


--
-- Name: ix_chat_sessions_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_sessions_agent_id ON public.chat_sessions USING btree (agent_id);


--
-- Name: ix_e2ag_approvals_event_log_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_e2ag_approvals_event_log_id ON public.e2ag_approvals USING btree (event_log_id);


--
-- Name: ix_e2ag_approvals_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_approvals_status ON public.e2ag_approvals USING btree (status);


--
-- Name: ix_e2ag_approvals_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_approvals_trace_id ON public.e2ag_approvals USING btree (trace_id);


--
-- Name: ix_e2ag_tool_grants_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_grants_agent_id ON public.e2ag_tool_grants USING btree (agent_id);


--
-- Name: ix_e2ag_tool_grants_event_log_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_grants_event_log_id ON public.e2ag_tool_grants USING btree (event_log_id);


--
-- Name: ix_e2ag_tool_grants_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_grants_task_id ON public.e2ag_tool_grants USING btree (task_id);


--
-- Name: ix_e2ag_tool_grants_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_grants_trace_id ON public.e2ag_tool_grants USING btree (trace_id);


--
-- Name: ix_e2ag_tool_invocations_event_log_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_invocations_event_log_id ON public.e2ag_tool_invocations USING btree (event_log_id);


--
-- Name: ix_e2ag_tool_invocations_grant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_invocations_grant_id ON public.e2ag_tool_invocations USING btree (grant_id);


--
-- Name: ix_e2ag_tool_invocations_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_invocations_task_id ON public.e2ag_tool_invocations USING btree (task_id);


--
-- Name: ix_e2ag_tool_invocations_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_e2ag_tool_invocations_trace_id ON public.e2ag_tool_invocations USING btree (trace_id);


--
-- Name: ix_event_dedup_claims_event_log_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_dedup_claims_event_log_id ON public.event_dedup_claims USING btree (event_log_id);


--
-- Name: ix_event_dedup_claims_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_dedup_claims_expires_at ON public.event_dedup_claims USING btree (expires_at);


--
-- Name: ix_event_logs_dedup_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_logs_dedup_hash ON public.event_logs USING btree (dedup_hash);


--
-- Name: ix_event_logs_trace_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_event_logs_trace_id ON public.event_logs USING btree (trace_id);


--
-- Name: ix_subscriptions_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_subscriptions_agent_id ON public.subscriptions USING btree (agent_id);


--
-- PostgreSQL database dump complete
--

\unrestrict HPny4PnLb75qs5I6fiksc81Hzbxzhpy1IpTLGI0qDaE34MZlh0KefuLwXU8IXk8
