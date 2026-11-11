"""Parameterised planning model; outputs estimates, never measured capacity."""
import argparse,json,math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def calculate(p):
    assert p['runs_per_day']>0 and p['mean_attempt_seconds']>0
    assert 0<p['target_utilization']<1 and p['retry_extra_attempt_ratio']>=0
    attempts=p['runs_per_day']*(1+p['retry_extra_attempt_ratio'])
    offered_slots=attempts*p['mean_attempt_seconds']/86400
    peak_slots=offered_slots*p['peak_multiplier']
    required=math.ceil(peak_slots/p['target_utilization'])
    usable=min(p['max_execution_slots'],p['worker_hosts']*p['slots_per_host'],p['provider_account_slots_assumed'])
    events=attempts*(p['stage_events_per_attempt']+math.ceil(p['mean_attempt_seconds']/p['progress_interval_seconds']))
    audits=attempts*p['tool_calls_per_attempt']*p['audit_records_per_call']
    metadata_bytes=events*p['event_bytes']+audits*p['audit_record_bytes']
    detail_bytes=attempts*(p['artifact_mib_per_attempt']+p['log_mib_per_attempt'])*2**20
    factor=p['retention_days']*p['replication_factor']*p['storage_overhead_factor']
    renewals=usable/p['lease_renew_seconds']+usable/p['grant_renew_seconds']
    total_bytes=metadata_bytes+detail_bytes
    tokens_in=attempts*p['input_tokens_per_attempt'];tokens_out=attempts*p['output_tokens_per_attempt']
    prices=(p['input_price_per_million'],p['output_price_per_million'])
    cost=None if None in prices else (tokens_in*prices[0]+tokens_out*prices[1])/1e6
    # Worst case: all assigned slots stay busy throughout the outage, plus active-output reserve.
    outage_attempts_per_host=p['slots_per_host']*p['disconnection_hours']*3600/p['mean_attempt_seconds']
    bytes_per_attempt=total_bytes/attempts
    spool_gib=(outage_attempts_per_host+p['slots_per_host'])*bytes_per_attempt*p['spool_safety_factor']/2**30
    return {'id':p['id'],'basis':'calculated_assumption','attempts_per_day':round(attempts,3),'required_peak_slots':required,'configured_slots':p['max_execution_slots'],'effective_assumed_slots':usable,'capacity_sufficient_under_assumptions':usable>=required,'steady_slot_utilization':round(offered_slots/usable,4),'peak_slot_utilization':round(peak_slots/usable,4),'peak_requests_per_second':round(attempts*p['peak_multiplier']/86400,4),'events_per_day':round(events),'audit_rows_per_day':round(audits),'peak_event_audit_writes_per_second':round((events+audits)/86400*p['peak_multiplier'],3),'lease_and_grant_renewals_per_second_at_full_slots':round(renewals,3),'metadata_gib_retained_with_replica_overhead':round(metadata_bytes*p['retention_days']*p['metadata_replication_factor']*p['storage_overhead_factor']/2**30,2),'artifact_log_gib_retained_with_replica_overhead':round(detail_bytes*factor/2**30,2),'average_detail_mbps':round(detail_bytes*8/86400/1e6,3),'spool_gib_per_host_24h_full_occupancy':round(spool_gib,2),'worker_ram_gib_each_minimum':math.ceil(p['slots_per_host']*p['worker_memory_gib_per_slot']/p['target_utilization']),'worker_cpu_each_minimum':math.ceil(p['slots_per_host']*p['worker_cpu_cores_per_slot']/p['target_utilization']),'tokens_in_per_day':round(tokens_in),'tokens_out_per_day':round(tokens_out),'model_cost_per_day':cost,'measured_throughput':None}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--profiles',type=Path,default=ROOT/'contracts/v1/capacity-profiles.json');ap.add_argument('--output',type=Path);a=ap.parse_args()
    data=json.loads(a.profiles.read_text());result={'status':'design_estimate','profiles':[calculate({**data['shared'],**p}) for p in data['profiles']]}
    encoded=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(encoded,encoding='utf-8')
    else:print(encoded,end='')
if __name__=='__main__':main()
