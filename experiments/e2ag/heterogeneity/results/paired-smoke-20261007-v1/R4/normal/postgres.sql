--
-- PostgreSQL database dump
--

\restrict n7f8c844FVjCDTMP4kChMprcEXDiqEKzcO5lFXcdrqmGYuMCVojNeAScAXx8G2H

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
7573a9b0eef84c7cba9048112aca23a3	file-agent	2d96dc256c96	c7c414cdf14647009897157e42df07ef	completed	{"role": "user", "messageId": "2638a5d5efc841269c381174fc2ea762", "parts": [{"kind": "text", "text": "[Event type=http.received source=http/fixture subject=delivery-1]"}, {"kind": "data", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}}], "_source": {"kind": "cloudevent", "id": "delivery-1", "type": "http.received", "source": "http/fixture"}, "_e2ag": {"trace_id": "c7c414cdf14647009897157e42df07ef"}}	[{"qualification_call_status": 200, "response": {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "stored"}]}}}]		2026-10-07 08:03:34.268274+00	2026-10-07 08:03:34.268274+00
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
file-agent	Fixed file workflow	task		agent				[]		[]		{"governance": {"allowed_event_sources": ["http/fixture"], "allowed_event_types": ["http.received"], "allowed_actions": ["report.create", "secret.read"], "require_action_declaration": true, "allowed_tools": ["report.write"], "argument_constraints": {"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}}}	{}	2026-10-07 08:03:31.494121+00
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
7ea5400c53624c75bea923f3b71890c8	a4fd9728c694dbbf6af85a61909b4f66918de19926069637a27d37df7f851f07	c7c414cdf14647009897157e42df07ef	7573a9b0eef84c7cba9048112aca23a3	2d96dc256c96	file-agent	file-tool	["report.write"]	{"version": 2, "tools": {"report.write": {"resource": {"type": "string", "enum": ["report/allowed"]}, "report": {"type": "string", "minLength": 1, "maxLength": 128}}}}	revoked	2026-10-07 08:18:34.304126+00	1	2026-10-07 08:03:35.174475+00	2026-10-07 08:03:34.306638+00
\.


--
-- Data for Name: e2ag_tool_invocations; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.e2ag_tool_invocations (id, grant_id, event_log_id, trace_id, task_id, method, tool_name, request_sha256, state, upstream_status, response_sha256, created_at, finished_at) FROM stdin;
573ab0aa9b624a3c950fa2fd29707174	7ea5400c53624c75bea923f3b71890c8	2d96dc256c96	c7c414cdf14647009897157e42df07ef	7573a9b0eef84c7cba9048112aca23a3	initialize		cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130	response_received	200	29be6f4a4b2718b68520477c42bd284301b18511b2d48e3ea09f34fe807fea31	2026-10-07 08:03:34.354765+00	2026-10-07 08:03:34.679525+00
4489408d15aa46e7a23d9fe7894d4212	7ea5400c53624c75bea923f3b71890c8	2d96dc256c96	c7c414cdf14647009897157e42df07ef	7573a9b0eef84c7cba9048112aca23a3	notifications/initialized		59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e	response_received	202	e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855	2026-10-07 08:03:34.702619+00	2026-10-07 08:03:34.90785+00
bbdae16183234c839ff4ea5315cc2611	7ea5400c53624c75bea923f3b71890c8	2d96dc256c96	c7c414cdf14647009897157e42df07ef	7573a9b0eef84c7cba9048112aca23a3	tools/call	report.write	18dcae54996fed84d709c29b69ffc1313a24c46c776c2e3694731580a761b9c3	response_received	200	c9d4033871da03574ac9cbd2fe532af0a5da195bb30ac83ddf00908dfadb8ef6	2026-10-07 08:03:34.939861+00	2026-10-07 08:03:35.167147+00
\.


--
-- Data for Name: event_dedup_claims; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_dedup_claims (dedup_hash, owner_token, event_log_id, expires_at, created_at) FROM stdin;
ccacbca383e5739ff37fddfcdd701ef5490b45d6108f9ee3b812d2a29dec22f3	ebab992e7e70442098ecd4d56f696f7e	2d96dc256c96	2026-10-07 09:03:34.231223+00	2026-10-07 08:03:34.232221+00
\.


--
-- Data for Name: event_logs; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.event_logs (id, source, event_type, subject, cloud_event, matched_agent_ids, status, trace_id, contract_decision, policy_decision, audit_chain, created_at, retry_count, max_retries, next_retry_at, error_message, dedup_hash) FROM stdin;
2d96dc256c96	http/fixture	http.received	delivery-1	{"specversion": "1.0", "id": "delivery-1", "source": "http/fixture", "type": "http.received", "subject": "delivery-1", "data": {"text": "governance receipt", "environment": "production", "requested_action": "report.create"}, "deliverysubject": "Qualification task", "deliverydigest": "6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484"}	["file-agent"]	dispatched	c7c414cdf14647009897157e42df07ef	{"mode": "enforce", "stage": "contract", "decision": "allow", "reason_codes": ["CONTRACT_BOUND"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "http-qualification", "latency_us": 144, "evidence": {"source_pattern": "http/fixture", "event_type": "http.received"}}	{"mode": "enforce", "stage": "policy", "decision": "allow", "reason_codes": ["POLICY_ALLOWED"], "policy_version": "e2ag-2026-08-12.1", "contract_type": "http-qualification", "latency_us": 92, "evidence": {"action": "report.create", "tool": "", "resource": "", "environment": "production"}, "effective_decision": "allow"}	[{"stage": "contract", "outcome": "allow", "evidence": {"stage": "contract", "decision": "allow", "evidence": {"event_type": "http.received", "source_pattern": "http/fixture"}, "latency_us": 144, "reason_codes": ["CONTRACT_BOUND"], "contract_type": "http-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 0, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "7077cbedb01de4cd5aadfcce1e874c6f6f7597d833a78ec2bf1d51fac7702dba", "previous_hash": ""}, {"stage": "policy", "outcome": "allow", "evidence": {"stage": "policy", "decision": "allow", "evidence": {"tool": "", "action": "report.create", "resource": "", "environment": "production"}, "latency_us": 92, "reason_codes": ["POLICY_ALLOWED"], "contract_type": "http-qualification", "policy_version": "e2ag-2026-08-12.1"}, "sequence": 1, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "b773e832219f157923cc51ac7876bddd6b4b879edec1a4769905ac958fd6a0f1", "previous_hash": "7077cbedb01de4cd5aadfcce1e874c6f6f7597d833a78ec2bf1d51fac7702dba"}, {"stage": "tool_grant", "outcome": "grant_issued", "evidence": {"task_id": "7573a9b0eef84c7cba9048112aca23a3", "agent_id": "file-agent", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "allowed_tools": ["report.write"], "mcp_server_id": "file-tool", "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 2, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "c0fff2a3cab001f624c42489f1a2b02be32c963da6d891cb660e6912c543c91a", "previous_hash": "b773e832219f157923cc51ac7876bddd6b4b879edec1a4769905ac958fd6a0f1"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "initialize", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "573ab0aa9b624a3c950fa2fd29707174", "request_sha256": "cabe8da3484c82db957143a35c85a46612dc07ddc233fca3e3b991bdc3e90130"}, "sequence": 3, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "55db3d36716c9548fe66866976d7e616e766f985846555f2700d2d6ab712e110", "previous_hash": "c0fff2a3cab001f624c42489f1a2b02be32c963da6d891cb660e6912c543c91a"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "initialize", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "agent_id": "file-agent", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "573ab0aa9b624a3c950fa2fd29707174", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 4, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "887e5a5f332a01c63fe3f1ecdd9f693a508f0ad13f8cea87319dc17a1e5b4b6f", "previous_hash": "55db3d36716c9548fe66866976d7e616e766f985846555f2700d2d6ab712e110"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "", "method": "notifications/initialized", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "4489408d15aa46e7a23d9fe7894d4212", "request_sha256": "59951ca0b212b103876fa23a9e58bcbbc8fbcc0120e3f5ee7214461e2bd1cd5e"}, "sequence": 5, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "7bde32f2b6144fce9570c473709f87fd9cd149dd3c3f71b3e6027d60f54638f9", "previous_hash": "887e5a5f332a01c63fe3f1ecdd9f693a508f0ad13f8cea87319dc17a1e5b4b6f"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "", "method": "notifications/initialized", "reason": "MCP_CONTROL_METHOD_ALLOWED", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "agent_id": "file-agent", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "4489408d15aa46e7a23d9fe7894d4212", "mcp_server_id": "file-tool", "upstream_status": 202, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 6, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "b139424d37ef3010b0c19579ac641b0d333a9aa079c678fb2cbe7efae89cc70e", "previous_hash": "7bde32f2b6144fce9570c473709f87fd9cd149dd3c3f71b3e6027d60f54638f9"}, {"stage": "tool_intent", "outcome": "outcome_unknown", "evidence": {"tool": "report.write", "method": "tools/call", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "bbdae16183234c839ff4ea5315cc2611", "request_sha256": "18dcae54996fed84d709c29b69ffc1313a24c46c776c2e3694731580a761b9c3"}, "sequence": 7, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "e02b0b347a6e790316e777fed12bd570ee570acc1a32492fefd7d055755abd1a", "previous_hash": "b139424d37ef3010b0c19579ac641b0d333a9aa079c678fb2cbe7efae89cc70e"}, {"stage": "tool_call", "outcome": "allow", "evidence": {"tool": "report.write", "method": "tools/call", "reason": "MCP_ARGUMENTS_GRANTED", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "agent_id": "file-agent", "grant_id": "7ea5400c53624c75bea923f3b71890c8", "invocation_id": "bbdae16183234c839ff4ea5315cc2611", "mcp_server_id": "file-tool", "upstream_status": 200, "argument_constraints_sha256": "6b7f33942674dbcc26bacf7853d132882aafe5b989cf50db926b055cae6315af"}, "sequence": 8, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "13df5e8595d6035d33f2e1eb8ba69325434551de07df096f60ab9870002d3ccc", "previous_hash": "e02b0b347a6e790316e777fed12bd570ee570acc1a32492fefd7d055755abd1a"}, {"stage": "tool_grant", "outcome": "grant_revoked", "evidence": {"reason": "task_lifecycle", "task_id": "7573a9b0eef84c7cba9048112aca23a3", "grant_id": "7ea5400c53624c75bea923f3b71890c8"}, "sequence": 9, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "03239a8d33eec3b3eee1ed8680fb056d0e1be354c38e64b2bde8b3072650b196", "previous_hash": "13df5e8595d6035d33f2e1eb8ba69325434551de07df096f60ab9870002d3ccc"}, {"stage": "a2a_task", "outcome": "completed", "evidence": {"task_id": "7573a9b0eef84c7cba9048112aca23a3", "agent_id": "file-agent"}, "sequence": 10, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "d72c520bcd68a90d9c1b646f6a949a3c615adb24d73c35c718602ac7b38ca0ac", "previous_hash": "03239a8d33eec3b3eee1ed8680fb056d0e1be354c38e64b2bde8b3072650b196"}, {"stage": "dispatch", "outcome": "dispatched", "evidence": {"error": "", "matched_agent_ids": ["file-agent"]}, "sequence": 11, "trace_id": "c7c414cdf14647009897157e42df07ef", "entry_hash": "fdf7d5060773b05d5be547d6c330bf2f778aad81136bea954001d12d1a16176c", "previous_hash": "d72c520bcd68a90d9c1b646f6a949a3c615adb24d73c35c718602ac7b38ca0ac"}]	2026-10-07 08:03:34.237243+00	0	3	2026-10-07 08:04:34.235732+00		ccacbca383e5739ff37fddfcdd701ef5490b45d6108f9ee3b812d2a29dec22f3
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
71f6c1722976819bb97eafa74793c6f67c79f96fb1219fb6e6b92db2d806fabe	http/fixture	delivery-1	6d028e71efa100c6d4a7239b568ca60b2bfb4ecda292d4d7c33a95ed6b724484	recorded	2d96dc256c96
\.


--
-- Data for Name: mcp_servers; Type: TABLE DATA; Schema: public; Owner: -
--

COPY public.mcp_servers (id, name, transport, url, headers, command, args, env, created_at) FROM stdin;
file-tool	receipt	streamable_http	http://127.0.0.1:54578/mcp	{}		[]	{}	2026-10-07 08:03:31.502713+00
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
ed18cca5dfd9	file-agent	http/*	["http.received"]	{}		t	2026-10-07 08:03:31.50882+00
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

\unrestrict n7f8c844FVjCDTMP4kChMprcEXDiqEKzcO5lFXcdrqmGYuMCVojNeAScAXx8G2H
