--
-- PostgreSQL database dump
--

\restrict L55jJPTQssSfyjC3dHc9eBiQu8Y5PdzwqFZpJuGwolbQwEuUYJHw5vgPbJ6aGHr

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
c35723399d0f47b681fd9831c415c2c5	file-agent	b07562ccbd7b	029cba30b68c424cb7a1b4da9dbc89f9	failed	{"role": "user", "messageId": "3bed0296693c494dba23cb86a19fd33a", "parts": [{"kind": "text", "text": "[Event type=http.received source=http/fixture subject=delivery-1]"}, {"kind": "data", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}}], "_source": {"kind": "cloudevent", "id": "delivery-1", "type": "http.received", "source": "http/fixture"}, "_e2ag": {"trace_id": "029cba30b68c424cb7a1b4da9dbc89f9"}}	[{"qualification_call_status": 403, "response": {"jsonrpc": "2.0", "id": 2, "error": {"code": -32001, "message": "E2AG tool grant rejected", "data": {"reason": "TOOL_GRANT_NOT_ACTIVE"}}}}]		2026-10-07 08:14:34.079832+00	2026-10-07 08:14:34.079832+00
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
file-agent	Fixed file workflow	task		agent				[]		[]		{"governance": {"allowed_event_sources": ["http/fixture"], "allowed_event_types": ["http.received"], "allowed_actions": ["report.create", "secret.read"], "require_action_declaration": true, "allowed_tools": ["report.write"], "argument_constraints": {"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}}}	{}	2026-10-07 08:14:31.430264+00
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
5d0011b3a03d4c96b6f66e00680ee9f5	e7300f57e3048e9f0c8236254ce9afbe221efc7895913de2491016d575e73c5b	029cba30b68c424cb7a1b4da9dbc89f9	c35723399d0f47b681fd9831c415c2c5	b07562ccbd7b	file-agent	file-tool	["report.write"]	{"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}	revoked	2026-10-07 08:29:34.122808+00	1	2026-10-07 08:14:34.870617+00	2026-10-07 08:14:34.124811+00
\.


--
-- Data for Name: e2ag_tool_invocations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_invocations (id, grant_id, event_log_id, trace_id, task_id, method, tool_name, request_sha256, state, upstream_status, response_sha256, created_at, finished_at) FROM stdin;
42c77e24123a49b5a5ff1a1b2911c03c	5d0011b3a03d4c96b6f66e00680ee9f5	b07562ccbd7b	029cba30b68c424cb7a1b4da9dbc89f9	c35723399d0f47b681fd9831c415c2c5	initialize		cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130	response_received	200	29be6f4a4b2718b68520477c42bd284301b18511b2d48e3ea09f34fe807fea31	2026-10-07 08:14:34.173739+00	2026-10-07 08:14:34.554697+00
7440da783c1542a2bbde6773a27e38fc	5d0011b3a03d4c96b6f66e00680ee9f5	b07562ccbd7b	029cba30b68c424cb7a1b4da9dbc89f9	c35723399d0f47b681fd9831c415c2c5	notifications/initialized		59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e	response_received	202	e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855	2026-10-07 08:14:34.589507+00	2026-10-07 08:14:34.818558+00
\.


--
-- Data for Name: event_dedup_claims; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_dedup_claims (dedup_hash, owner_token, event_log_id, expires_at, created_at) FROM stdin;
ccacbca383e5739ff37fddfcdd701ef5490b45d6108f9ee3b812d2a29dec22f3	1ab07e47a68841b9b56357f0736660d8	b07562ccbd7b	2026-10-07 09:14:34.033821+00	2026-10-07 08:14:34.03582+00
\.


--
-- Data for Name: event_logs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_logs (id, source, event_type, subject, cloud_event, matched_agent_ids, status, trace_id, contract_decision, policy_decision, audit_chain, created_at, retry_count, max_retries, next_retry_at, error_message, dedup_hash) FROM stdin;
b07562ccbd7b	http/fixture	http.received	delivery-1	{"specversion": "1.0", "id": "delivery-1", "source": "http/fixture", "type": "http.received", "subject": "delivery-1", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}, "deliverysubject": "Qualification task", "deliverydigest": "6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484"}	["file-agent"]	failed	029cba30b68c424cb7a1b4da9dbc89f9	{"mode": "enforce", "stage": "contract", "decision": "allow", "reason_codes": ["CONTRACT_BOUND"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "http-qualification", "latency_us": 301, "evidence": {"source_pattern": "http/fixture", "event_type": "http.received"}}	{"mode": "enforce", "stage": "policy", "decision": "allow", "reason_codes": ["POLICY_ALLOWED"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "http-qualification", "latency_us": 226, "evidence": {"action": "report.create", "tool": "", "resource": "", "environment": "production"}, "effective_decision": "allow"}	[{"stage": "contract", "outcome": "allow", "evidence": {"stage": "contract", "decision": "allow", "evidence": {"event_type": "http.received", "source_pattern": "http/fixture"}, "latency_us": 301, "reason_codes": ["CONTRACT_BOUND"], "contract_type": "http-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 0, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "742abdd1916f3b31c3d6e83e1e5a1709e85b8605828dffa76e2971d094b90045", "previous_hash": ""}, {"stage": "policy", "outcome": "allow", "evidence": {"stage": "policy", "decision": "allow", "evidence": {"tool": "", "action": "report.create", "resource": "", "environment": "production"}, "latency_us": 226, "reason_codes": ["POLICY_ALLOWED"], "contract_type": "http-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 1, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "e90d1c9d1d130e569f88123a12ea198f73d7c2804a23778a0137708f5a59734d", "previous_hash": "742abdd1916f3b31c3d6e83e1e5a1709e85b8605828dffa76e2971d094b90045"}, {"stage": "tool_grant", "outcome": "grant_issued", "evidence": {"task_id": "c35723399d0f47b681fd9831c415c2c5", "agent_id": "file-agent", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "allowed_tools": ["report.write"], "mcp_server_id": "file-tool", "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 2, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "15d33bc88e3f3ef789001cbc6d8d86a11518a0d491570875ba1fa9fe17d74081", "previous_hash": "e90d1c9d1d130e569f88123a12ea198f73d7c2804a23778a0137708f5a59734d"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "initialize", "task_id": "c35723399d0f47b681fd9831c415c2c5", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "invocation_id": "42c77e24123a49b5a5ff1a1b2911c03c", "request_sha256": "cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130"}, "sequence": 3, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "8fa22e4a5e8ebe6eaf29f454d8831827ffc645bbefbc6f6ccfa047ad983cb872", "previous_hash": "15d33bc88e3f3ef789001cbc6d8d86a11518a0d491570875ba1fa9fe17d74081"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "initialize", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "c35723399d0f47b681fd9831c415c2c5", "agent_id": "file-agent", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "invocation_id": "42c77e24123a49b5a5ff1a1b2911c03c", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 4, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "745d76a17b8dcd0cc411f3461e715eed3973a74565250d711f6bde99e077d37a", "previous_hash": "8fa22e4a5e8ebe6eaf29f454d8831827ffc645bbefbc6f6ccfa047ad983cb872"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "notifications/initialized", "task_id": "c35723399d0f47b681fd9831c415c2c5", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "invocation_id": "7440da783c1542a2bbde6773a27e38fc", "request_sha256": "59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e"}, "sequence": 5, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "6a0e85aa6edc1231fecd34ac98eccbe90d4975e36c39d4ff70b41c07bf2685ab", "previous_hash": "745d76a17b8dcd0cc411f3461e715eed3973a74565250d711f6bde99e077d37a"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "notifications/initialized", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "c35723399d0f47b681fd9831c415c2c5", "agent_id": "file-agent", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "invocation_id": "7440da783c1542a2bbde6773a27e38fc", "mcp_server_id": "file-tool", "upstream_status": 202, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 6, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "ae7eb0c23694b2542a3a9daa40b5a7489b765518794396267d705f56dfdb55d4", "previous_hash": "6a0e85aa6edc1231fecd34ac98eccbe90d4975e36c39d4ff70b41c07bf2685ab"}, {"stage": "tool_grant", "outcome": "grant_revoked", "evidence": {"reason": "task_lifecycle", "task_id": "c35723399d0f47b681fd9831c415c2c5", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5"}, "sequence": 7, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "56b325d0396e09c9edebb7b9cbb528bc327092ccfb4f8f45d7fb4463e0314bd9", "previous_hash": "ae7eb0c23694b2542a3a9daa40b5a7489b765518794396267d705f56dfdb55d4"}, {"stage": "tool_call", "outcome": "deny", "evidence": {"tool": "report.write", "method": "tools/call", "reason": "TOOL_GRANT_NOT_ACTIVE", "task_id": "c35723399d0f47b681fd9831c415c2c5", "agent_id": "file-agent", "grant_id": "5d0011b3a03d4c96b6f66e00680ee9f5", "mcp_server_id": "file-tool", "upstream_status": null, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 8, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "f4aa24f2084f1d14fc7064072fb4e1b14b36029891dc160319fa059c76b44bd9", "previous_hash": "56b325d0396e09c9edebb7b9cbb528bc327092ccfb4f8f45d7fb4463e0314bd9"}, {"stage": "a2a_task", "outcome": "failed", "evidence": {"task_id": "c35723399d0f47b681fd9831c415c2c5", "agent_id": "file-agent"}, "sequence": 9, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "0b32cea09713a74f5530c68e9185210e764ec0faca976ccc33dc651d37d109e5", "previous_hash": "f4aa24f2084f1d14fc7064072fb4e1b14b36029891dc160319fa059c76b44bd9"}, {"stage": "dispatch", "outcome": "failed", "evidence": {"error": "Agent file-agent: ", "matched_agent_ids": ["file-agent"]}, "sequence": 10, "trace_id": "029cba30b68c424cb7a1b4da9dbc89f9", "entry_hash": "b757d410ce06659b21ed442982ce58197e6c5641b0a01b7117ad76e2693e55dc", "previous_hash": "0b32cea09713a74f5530c68e9185210e764ec0faca976ccc33dc651d37d109e5"}]	2026-10-07 08:14:34.041819+00	0	3	2026-10-07 08:15:34.039817+00	Agent file-agent: 	ccacbca383e5739ff37fddfcdd701ef5490b45d6108f9ee3b812d2a29dec22f3
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
71f6c1722976819bb97eafa74793c6f67c79f96fb1219fb6e6b92db2d806fabe	http/fixture	delivery-1	6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484	recorded	b07562ccbd7b
\.


--
-- Data for Name: mcp_servers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.mcp_servers (id, name, transport, url, headers, command, args, env, created_at) FROM stdin;
file-tool	receipt	streamable_http	http://127.0.0.1:64219/mcp	{}		[]	{}	2026-10-07 08:14:31.442811+00
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
f71119b84cbf	file-agent	http/*	["http.received"]	{}		t	2026-10-07 08:14:31.451353+00
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

\unrestrict L55jJPTQssSfyjC3dHc9eBiQu8Y5PdzwqFZpJuGwolbQwEuUYJHw5vgPbJ6aGHr
