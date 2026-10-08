--
-- PostgreSQL database dump
--

\restrict xBBLDopcwu8SjKqhEPiKaCFiaP1Cm6ia7HrYYqKJ23CMMpY4yPlGhasOtbxJxqc

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
8b02a6060e7148daab4a5103acdee14a	file-agent	d30800542dfc	048c7cc733304c27961b51a80a4f20ae	completed	{"role": "user", "messageId": "221c4a26ddf7463da9c506f97fb1cd28", "parts": [{"kind": "text", "text": "[Event type=file.received source=file/fixture subject=delivery-1]"}, {"kind": "data", "data": {"text": "governance receipt", "environment": "production", "requested_action": "secret.read"}}], "_source": {"kind": "cloudevent", "id": "delivery-1", "type": "file.received", "source": "file/fixture"}, "_e2ag": {"trace_id": "048c7cc733304c27961b51a80a4f20ae"}}	[{"qualification_call_status": 200, "response": {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "stored"}]}}}]		2026-10-07 07:40:05.565811+00	2026-10-07 07:40:05.565811+00
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
file-agent	Fixed file workflow	task		agent				[]		[]		{"governance": {"allowed_event_sources": ["file/fixture"], "allowed_event_types": ["file.received"], "allowed_actions": ["report.create", "secret.read"], "require_action_declaration": true, "allowed_tools": ["report.write"], "argument_constraints": {"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}}}	{}	2026-10-07 07:40:00.467094+00
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
8eed1f390aa14438b5ae07545cf35152	d30800542dfc	048c7cc733304c27961b51a80a4f20ae	approved	2026-10-07 08:40:02.617817+00	2026-10-07 07:40:05.498343+00	qualification-reviewer		2026-10-07 07:40:02.619824+00
\.


--
-- Data for Name: e2ag_tool_grants; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_grants (id, token_hash, trace_id, task_id, event_log_id, agent_id, mcp_server_id, allowed_tools, argument_constraints, status, expires_at, call_count, last_used_at, created_at) FROM stdin;
781e208322b449aa880ae1ec5be23774	0e0692bb23cacdb42d677309709a1290bdcf52c8a0c5afebb59b27ffed2ecefa	048c7cc733304c27961b51a80a4f20ae	8b02a6060e7148daab4a5103acdee14a	d30800542dfc	file-agent	file-tool	["report.write"]	{"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}	revoked	2026-10-07 07:55:05.618769+00	1	2026-10-07 07:40:06.679782+00	2026-10-07 07:40:05.620771+00
\.


--
-- Data for Name: e2ag_tool_invocations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_invocations (id, grant_id, event_log_id, trace_id, task_id, method, tool_name, request_sha256, state, upstream_status, response_sha256, created_at, finished_at) FROM stdin;
d09f36b7048f4cc2b077cf2e4bbed314	781e208322b449aa880ae1ec5be23774	d30800542dfc	048c7cc733304c27961b51a80a4f20ae	8b02a6060e7148daab4a5103acdee14a	initialize		cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130	response_received	200	29be6f4a4b2718b68520477c42bd284301b18511b2d48e3ea09f34fe807fea31	2026-10-07 07:40:05.686007+00	2026-10-07 07:40:06.084982+00
fd0e52c08e604a1baac14c6d21830c0a	781e208322b449aa880ae1ec5be23774	d30800542dfc	048c7cc733304c27961b51a80a4f20ae	8b02a6060e7148daab4a5103acdee14a	notifications/initialized		59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e	response_received	202	e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855	2026-10-07 07:40:06.132651+00	2026-10-07 07:40:06.367394+00
4db88e1a09fd45eeab71330e59a3dc82	781e208322b449aa880ae1ec5be23774	d30800542dfc	048c7cc733304c27961b51a80a4f20ae	8b02a6060e7148daab4a5103acdee14a	tools/call	report.write	18dcae54996fed84d709c29b69ffc1313a24c46c776c2e3694731580a761b9c3	response_received	200	c9d4033871da03574ac9cbd2fe532af0a5da195bb30ac83ddf00908dfadb8ef6	2026-10-07 07:40:06.394599+00	2026-10-07 07:40:06.668735+00
\.


--
-- Data for Name: event_dedup_claims; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_dedup_claims (dedup_hash, owner_token, event_log_id, expires_at, created_at) FROM stdin;
cadc4c1c1c95e6fc3ef2f32f04e30b6a8fa2239d66fc49ad552d29e5e86aca9c	867d336f82c74e6c97752bc74dc6a493	d30800542dfc	2026-10-07 08:40:02.539157+00	2026-10-07 07:40:02.541156+00
\.


--
-- Data for Name: event_logs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_logs (id, source, event_type, subject, cloud_event, matched_agent_ids, status, trace_id, contract_decision, policy_decision, audit_chain, created_at, retry_count, max_retries, next_retry_at, error_message, dedup_hash) FROM stdin;
d30800542dfc	file/fixture	file.received	delivery-1	{"specversion": "1.0", "id": "delivery-1", "source": "file/fixture", "type": "file.received", "subject": "delivery-1", "data": {"text": "governance receipt", "environment": "production", "requested_action": "secret.read"}, "deliverysubject": "Qualification task", "deliverydigest": "931d3c7cc7c875db96c74cfad2c92bccd31ce3d3b5314052770750b3604cea66"}	["file-agent"]	dispatched	048c7cc733304c27961b51a80a4f20ae	{"mode": "enforce", "stage": "contract", "decision": "allow", "reason_codes": ["CONTRACT_BOUND"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "file-qualification", "latency_us": 247, "evidence": {"source_pattern": "file/fixture", "event_type": "file.received"}}	{"mode": "enforce", "stage": "policy", "decision": "approval_required", "reason_codes": ["POLICY_PRODUCTION_SENSITIVE_ACTION"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "file-qualification", "latency_us": 261, "evidence": {"action": "secret.read", "tool": "", "resource": "", "environment": "production"}, "effective_decision": "approval_required"}	[{"stage": "contract", "outcome": "allow", "evidence": {"stage": "contract", "decision": "allow", "evidence": {"event_type": "file.received", "source_pattern": "file/fixture"}, "latency_us": 247, "reason_codes": ["CONTRACT_BOUND"], "contract_type": "file-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 0, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "03d6049fb0133e1b8b73322cc2cfa55a83a8e0d3147ef1eb1b9f09aa84afcc71", "previous_hash": ""}, {"stage": "policy", "outcome": "approval_required", "evidence": {"stage": "policy", "decision": "approval_required", "evidence": {"tool": "", "action": "secret.read", "resource": "", "environment": "production"}, "latency_us": 261, "reason_codes": ["POLICY_PRODUCTION_SENSITIVE_ACTION"], "contract_type": "file-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 1, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "6f556f5327d449358209bedc78a957dce3a47a5ae67522a17acd72a5d6a47db3", "previous_hash": "03d6049fb0133e1b8b73322cc2cfa55a83a8e0d3147ef1eb1b9f09aa84afcc71"}, {"stage": "dispatch", "outcome": "blocked", "evidence": {"reason_codes": ["POLICY_PRODUCTION_SENSITIVE_ACTION"]}, "sequence": 2, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "2fae9949dfa89fca81c958dc6be9fc0b483e579375a6393571f4df60391e31b7", "previous_hash": "6f556f5327d449358209bedc78a957dce3a47a5ae67522a17acd72a5d6a47db3"}, {"stage": "approval", "outcome": "pending", "evidence": {"expires_at": "2026-10-07T08:40:02.617817+00:00", "approval_id": "8eed1f390aa14438b5ae07545cf35152"}, "sequence": 3, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "a24f2db40025e1c8b18456531cd7dadb9034991111628eb4fbd5db506ea70f02", "previous_hash": "2fae9949dfa89fca81c958dc6be9fc0b483e579375a6393571f4df60391e31b7"}, {"stage": "approval", "outcome": "approved", "evidence": {"actor": "qualification-reviewer", "reason": "", "approval_id": "8eed1f390aa14438b5ae07545cf35152"}, "sequence": 4, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "2a58fee90a751927cff1fb136261a87f0ccf7bc001cf7e0b3166be2a080f32be", "previous_hash": "a24f2db40025e1c8b18456531cd7dadb9034991111628eb4fbd5db506ea70f02"}, {"stage": "tool_grant", "outcome": "grant_issued", "evidence": {"task_id": "8b02a6060e7148daab4a5103acdee14a", "agent_id": "file-agent", "grant_id": "781e208322b449aa880ae1ec5be23774", "allowed_tools": ["report.write"], "mcp_server_id": "file-tool", "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 5, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "ed800dfa2ab2bf7312818c0aa47a35637dd1d4b5c2fdba63b09fbe41b940e4eb", "previous_hash": "2a58fee90a751927cff1fb136261a87f0ccf7bc001cf7e0b3166be2a080f32be"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "initialize", "task_id": "8b02a6060e7148daab4a5103acdee14a", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "d09f36b7048f4cc2b077cf2e4bbed314", "request_sha256": "cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130"}, "sequence": 6, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "950966e6352802dae126b1f7b308bffefc04d857b219f0aa4994dda7062f4558", "previous_hash": "ed800dfa2ab2bf7312818c0aa47a35637dd1d4b5c2fdba63b09fbe41b940e4eb"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "initialize", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "8b02a6060e7148daab4a5103acdee14a", "agent_id": "file-agent", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "d09f36b7048f4cc2b077cf2e4bbed314", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 7, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "e83731990f118bf338164b42cca4b3c9794666716fbbde2fc506242408829d25", "previous_hash": "950966e6352802dae126b1f7b308bffefc04d857b219f0aa4994dda7062f4558"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "notifications/initialized", "task_id": "8b02a6060e7148daab4a5103acdee14a", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "fd0e52c08e604a1baac14c6d21830c0a", "request_sha256": "59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e"}, "sequence": 8, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "2b6b715fae3a7731b13de5bac433558b6bd3c0e3525782f5486f81ddf54f2cb0", "previous_hash": "e83731990f118bf338164b42cca4b3c9794666716fbbde2fc506242408829d25"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "notifications/initialized", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "8b02a6060e7148daab4a5103acdee14a", "agent_id": "file-agent", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "fd0e52c08e604a1baac14c6d21830c0a", "mcp_server_id": "file-tool", "upstream_status": 202, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 9, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "c9d1d75312d915a5e137fbd760a0643817ea14a5e114f1fcdfa24939377b8044", "previous_hash": "2b6b715fae3a7731b13de5bac433558b6bd3c0e3525782f5486f81ddf54f2cb0"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "report.write", "method": "tools/call", "task_id": "8b02a6060e7148daab4a5103acdee14a", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "4db88e1a09fd45eeab71330e59a3dc82", "request_sha256": "18dcae54996fed84d709c29b69ffc1313a24c46c776c2e3694731580a761b9c3"}, "sequence": 10, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "efbd2b9ac152bc71cbd3a919a011aeaed2bc6b050d3d88637354808288ad1929", "previous_hash": "c9d1d75312d915a5e137fbd760a0643817ea14a5e114f1fcdfa24939377b8044"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "report.write", "method": "tools/call", "reason": "MCP_ARGUMENTS_GRANTED", "task_id": "8b02a6060e7148daab4a5103acdee14a", "agent_id": "file-agent", "grant_id": "781e208322b449aa880ae1ec5be23774", "invocation_id": "4db88e1a09fd45eeab71330e59a3dc82", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 11, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "15a95d8d6e9bb74492cc7d7a8a836db6d6f325fa67f08db94dac72f045a6ec89", "previous_hash": "efbd2b9ac152bc71cbd3a919a011aeaed2bc6b050d3d88637354808288ad1929"}, {"stage": "tool_grant", "outcome": "grant_revoked", "evidence": {"reason": "task_lifecycle", "task_id": "8b02a6060e7148daab4a5103acdee14a", "grant_id": "781e208322b449aa880ae1ec5be23774"}, "sequence": 12, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "537ef977fc59e884c8489b455b524a1e7638a4817e9bca4e90f4812cf6445f7c", "previous_hash": "15a95d8d6e9bb74492cc7d7a8a836db6d6f325fa67f08db94dac72f045a6ec89"}, {"stage": "a2a_task", "outcome": "completed", "evidence": {"task_id": "8b02a6060e7148daab4a5103acdee14a", "agent_id": "file-agent", "after_approval": true}, "sequence": 13, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "53fe2b7337f7bf23b843eee2004e447190f0fd3e6dfb69de00889e05879b79dc", "previous_hash": "537ef977fc59e884c8489b455b524a1e7638a4817e9bca4e90f4812cf6445f7c"}, {"stage": "dispatch", "outcome": "dispatched", "evidence": {"error": "", "after_approval": true, "matched_agent_ids": ["file-agent"]}, "sequence": 14, "trace_id": "048c7cc733304c27961b51a80a4f20ae", "entry_hash": "9fe6cf86fb406968dd77ef64090606971e6f0c16f785c8c40c3f9111a7ef0302", "previous_hash": "53fe2b7337f7bf23b843eee2004e447190f0fd3e6dfb69de00889e05879b79dc"}]	2026-10-07 07:40:02.580462+00	0	3	\N		cadc4c1c1c95e6fc3ef2f32f04e30b6a8fa2239d66fc49ad552d29e5e86aca9c
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
b8664604abbbc241db60f5810a3d5fdeff0bcc89147a85236e6f00b11eb1cd74	file/fixture	delivery-1	931d3c7cc7c875db96c74cfad2c92bccd31ce3d3b5314052770750b3604cea66	recorded	d30800542dfc
\.


--
-- Data for Name: mcp_servers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.mcp_servers (id, name, transport, url, headers, command, args, env, created_at) FROM stdin;
file-tool	receipt	streamable_http	http://127.0.0.1:61595/mcp	{}		[]	{}	2026-10-07 07:40:00.472605+00
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
87b0090c5540	file-agent	file/*	["file.received"]	{}		t	2026-10-07 07:40:00.475262+00
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

\unrestrict xBBLDopcwu8SjKqhEPiKaCFiaP1Cm6ia7HrYYqKJ23CMMpY4yPlGhasOtbxJxqc
