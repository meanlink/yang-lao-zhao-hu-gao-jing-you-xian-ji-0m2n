from uuid import uuid4

from fastapi import HTTPException

from . import checklist, coverage, workflow
from .db import audit
from .schemas import AlertStatus, NewObservation

# Demo operations policy. A created task retains the SLA of the confirmed alert version.
RESPONSE_MINUTES = {3: 15, 2: 30, 1: 60}
CALLBACK_MINUTES = {'no_answer': 30, 'requested_callback': 60}
ACTIVE_RESPONSES = ('assigned', 'accepted')


def create_response(db, alert_id, data, actor):
    alert = workflow.lock_alert(db, alert_id)
    if alert['status'] != AlertStatus.CONFIRMED or alert['revision'] != data.revision:
        raise HTTPException(409, '请刷新并选择已复核的当前告警版本')
    if workflow.active_response(db, alert_id):
        raise HTTPException(409, '本告警已有一条在办响应任务')
    minutes = RESPONSE_MINUTES[alert['priority']]
    unit = db.execute('SELECT unit FROM residents WHERE id=%s', (alert['resident_id'],)).fetchone()['unit']
    coverage.reserve(db, unit, data.assigned_to, data.coverage_id, minutes)
    row = db.execute(
        '''INSERT INTO care_responses(alert_id, alert_revision, priority, assigned_to, directed_by, instruction, coverage_id, due_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,now() + make_interval(mins => %s)) RETURNING *''',
        (alert_id, alert['revision'], alert['priority'], data.assigned_to, actor, data.instruction, data.coverage_id, minutes),
    ).fetchone()
    audit(db, actor, 'care_response_assigned', 'care_response', row['id'], alert_id=alert_id, sla_minutes=minutes, coverage_id=data.coverage_id)
    return row


def transition_response(db, response_id, action, actor, note=None):
    row = workflow.lock_response(db, response_id)
    if action != 'cancel' and row['assigned_to'] != actor:
        raise HTTPException(403, '仅指定照护员可接单或填写处置结果')
    allowed = {'accept': ('assigned',), 'complete': ('accepted',), 'cancel': ACTIVE_RESPONSES}
    if row['status'] not in allowed[action]:
        raise HTTPException(409, '任务状态已经变化，请刷新队列')
    if action == 'accept':
        result = db.execute(
            "UPDATE care_responses SET status='accepted',accepted_at=now() WHERE id=%s RETURNING *",
            (response_id,),
        ).fetchone()
    else:
        if action == 'complete':
            checklist.ensure_complete(db, row)
        status = 'completed' if action == 'complete' else 'cancelled'
        result = db.execute(
            'UPDATE care_responses SET status=%s,finished_at=now(),finish_note=%s WHERE id=%s RETURNING *',
            (status, note, response_id),
        ).fetchone()
    audit(db, actor, f'care_response_{result["status"]}', 'care_response', response_id, alert_id=row['alert_id'])
    return result


def _recorded_transfer(db, request_key):
    return db.execute('SELECT * FROM care_response_transfers WHERE request_key=%s',
                      (request_key,)).fetchone()


def _transfer_repeat(db, response_id, recorded):
    if recorded['response_id'] != response_id:
        raise HTTPException(409, '该移交单号已用于其他响应，请刷新后重新操作')
    response = db.execute('SELECT * FROM care_responses WHERE id=%s', (response_id,)).fetchone()
    return {'response': response, 'transfer': recorded}


def transfer_response(db, response_id, data, actor):
    """Move an in-progress response to another on-duty carer in the same unit.

    The request key makes a retried submission idempotent. Capacity is counted only
    after the unit advisory lock is held, so a transfer racing a fresh dispatch for
    the last free slot leaves exactly one of them committed.
    """
    recorded = _recorded_transfer(db, data.request_key)
    if recorded:
        return _transfer_repeat(db, response_id, recorded)
    db.execute('SELECT pg_advisory_xact_lock(1049004, hashtext(%s))', (data.request_key,))
    recorded = _recorded_transfer(db, data.request_key)
    if recorded:
        return _transfer_repeat(db, response_id, recorded)
    row = workflow.lock_response(db, response_id)
    if row['status'] not in ACTIVE_RESPONSES:
        raise HTTPException(409, '仅在办响应可以移交，已处置或撤销的任务不能移交')
    unit = db.execute('''SELECT r.unit FROM alerts a JOIN residents r ON r.id=a.resident_id
                         WHERE a.id=%s''', (row['alert_id'],)).fetchone()['unit']
    coverage.lock_unit(db, unit)
    recorded = _recorded_transfer(db, data.request_key)
    if recorded:
        return _transfer_repeat(db, response_id, recorded)
    row = db.execute('SELECT * FROM care_responses WHERE id=%s FOR UPDATE', (response_id,)).fetchone()
    if row['status'] not in ACTIVE_RESPONSES:
        raise HTTPException(409, '仅在办响应可以移交，已处置或撤销的任务不能移交')
    target = coverage.lock_coverage(db, data.to_coverage_id)
    if target['unit'] != unit:
        raise HTTPException(409, '接手人须与原响应同区当班，请选择本区覆盖人员')
    if target['carer_id'] == row['assigned_to']:
        raise HTTPException(409, '该响应已由接手人继续处理，无需重复移交')
    eligible = db.execute(
        '''SELECT ends_at >= %s AS covers_deadline, ends_at > clock_timestamp() AS on_duty
           FROM shift_coverage WHERE id=%s''', (row['due_at'], target['id']),
    ).fetchone()
    if target['ended_at'] or not eligible['on_duty']:
        raise HTTPException(409, '接手人的当班已经结束，请选择其他人员')
    if not eligible['covers_deadline']:
        raise HTTPException(409, '接手人的当班无法覆盖原处置截止时刻，请选择其他人员')
    load = db.execute(
        '''SELECT count(*) AS total FROM care_responses WHERE assigned_to=%s
           AND status IN ('assigned','accepted')''', (target['carer_id'],),
    ).fetchone()['total']
    if load >= target['capacity']:
        raise HTTPException(409, '接手人的当班响应容量已满')
    updated = db.execute(
        'UPDATE care_responses SET assigned_to=%s,coverage_id=%s WHERE id=%s RETURNING *',
        (target['carer_id'], target['id'], response_id),
    ).fetchone()
    transfer = db.execute(
        '''INSERT INTO care_response_transfers(
               response_id,request_key,from_staff,to_staff,from_coverage_id,to_coverage_id,reason,moved_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
        (response_id, data.request_key, row['assigned_to'], target['carer_id'], row['coverage_id'],
         target['id'], data.reason, actor),
    ).fetchone()
    audit(db, actor, 'care_response_transferred', 'care_response', response_id,
          alert_id=row['alert_id'], from_staff=row['assigned_to'], to_staff=target['carer_id'],
          from_coverage_id=row['coverage_id'], to_coverage_id=target['id'], reason=data.reason)
    return {'response': updated, 'transfer': transfer}


def candidate_view(response, candidates):
    """Shape coverage rows into transfer candidates for one response."""
    view = []
    for row in candidates:
        if row['unit'] != response['unit']:
            continue
        reason = None
        if row['carer_id'] == response['assigned_to']:
            reason = '当前已是此响应的照护员'
        elif row['active_count'] >= row['capacity']:
            reason = '当班响应容量已满'
        elif row['ends_at'] < response['due_at']:
            reason = '当班无法覆盖原截止时刻'
        view.append({**{k: row[k] for k in ('id', 'carer_id', 'carer_name', 'shift', 'capacity',
                                            'active_count', 'ends_at')},
                     'covers_deadline': row['ends_at'] >= response['due_at'],
                     'eligible': reason is None, 'note': reason})
    return view


def transfer_candidates(db, response_id):
    response = db.execute(
        '''SELECT t.*,r.unit,r.name AS resident_name,r.room,s.name AS assignee,c.shift AS assigned_shift
           FROM care_responses t JOIN alerts a ON a.id=t.alert_id JOIN residents r ON r.id=a.resident_id
           JOIN staff s ON s.id=t.assigned_to LEFT JOIN shift_coverage c ON c.id=t.coverage_id
           WHERE t.id=%s''', (response_id,),
    ).fetchone()
    if response is None:
        raise HTTPException(404, '响应任务不存在')
    if response['status'] not in ACTIVE_RESPONSES:
        raise HTTPException(409, '仅在办响应可以移交')
    rows = coverage.on_duty_candidates(db, [response['unit']])
    return {'response': response, 'unit': response['unit'], 'items': candidate_view(response, rows)}


def open_contact(db, alert_id, data, actor):
    alert = workflow.lock_alert(db, alert_id)
    if alert['status'] not in (AlertStatus.CONFIRMED, AlertStatus.HANDOVER, AlertStatus.CLOSED):
        raise HTTPException(409, '告警经值班长复核后才能登记家属联络')
    row = db.execute(
        '''INSERT INTO family_contacts(alert_id,purpose,contact_role,opened_by) VALUES (%s,%s,%s,%s)
           ON CONFLICT (alert_id) DO NOTHING RETURNING *''',
        (alert_id, data.purpose, data.contact_role, actor),
    ).fetchone()
    if not row:
        raise HTTPException(409, '本条告警已有联络记录，请追加联系结果')
    audit(db, actor, 'family_contact_opened', 'family_contact', row['id'], alert_id=alert_id)
    return row


def record_attempt(db, contact_id, data, actor):
    contact = db.execute('SELECT status FROM family_contacts WHERE id=%s FOR UPDATE', (contact_id,)).fetchone()
    if not contact:
        raise HTTPException(404, '家属联络记录不存在')
    if contact['status'] == 'reached':
        raise HTTPException(409, '联络事项已确认接通')
    latest = db.execute('SELECT COALESCE(max(id),0) AS id FROM family_contact_attempts WHERE contact_id=%s', (contact_id,)).fetchone()
    if latest['id'] != data.expected_attempt_id:
        raise HTTPException(409, '已登记较新的联系记录，请刷新后再追加')
    attempt = db.execute(
        '''INSERT INTO family_contact_attempts(contact_id,recorded_by,channel,outcome,note)
           VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (contact_id, actor, data.channel, data.outcome, data.note),
    ).fetchone()
    minutes = CALLBACK_MINUTES.get(data.outcome)
    result = db.execute(
        '''UPDATE family_contacts SET status=%s, next_contact_at=now() + make_interval(mins => %s)
           WHERE id=%s RETURNING *''',
        ('reached' if minutes is None else 'callback', minutes, contact_id),
    ).fetchone()
    audit(db, actor, 'family_contact_attempt_recorded', 'family_contact', contact_id,
          attempt_id=attempt['id'], outcome=data.outcome)
    return result


def schedule_followup(db, handover_id, data, actor):
    handover = db.execute('SELECT status FROM handovers WHERE id=%s', (handover_id,)).fetchone()
    if not handover:
        raise HTTPException(404, '交班记录不存在')
    if handover['status'] != 'signed':
        raise HTTPException(409, '请先签收当前交班再安排复查')
    row = db.execute(
        '''INSERT INTO handover_followups(handover_id,scheduled_by,checklist,due_at)
           VALUES (%s,%s,%s,now() + make_interval(mins => %s))
           ON CONFLICT (handover_id) DO NOTHING RETURNING *''',
        (handover_id, actor, data.checklist, data.delay_minutes),
    ).fetchone()
    if not row:
        raise HTTPException(409, '该交班已经安排了接班复查')
    audit(db, actor, 'handover_followup_scheduled', 'handover_followup', row['id'], handover_id=handover_id)
    return row


def finish_followup(db, followup_id, data, actor):
    row = db.execute(
        '''SELECT f.*,a.resident_id FROM handover_followups f JOIN handovers h ON h.id=f.handover_id
           JOIN alerts a ON a.id=h.alert_id WHERE f.id=%s FOR UPDATE OF f''',
        (followup_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, '接班复查不存在')
    if row['status'] != 'scheduled':
        raise HTTPException(409, '该复查已经提交结果')
    observation_id = None
    if data.result == 'needs_attention':
        data_note = f'接班复查 #{followup_id}：{data.note}'[:600]
        observation = workflow.enter_observation(db, NewObservation(
            ref=f'OBS-FU-{followup_id}-{uuid4().hex[:8].upper()}',
            resident_id=row['resident_id'], author_note=data_note,
        ), actor)
        observation_id = observation['id']
    result = db.execute(
        '''UPDATE handover_followups SET status=%s,result_note=%s,completed_by=%s,
                  completed_at=now(),observation_id=%s WHERE id=%s RETURNING *''',
        ('referred' if observation_id else 'stable', data.note, actor, observation_id, followup_id),
    ).fetchone()
    audit(db, actor, 'handover_followup_completed', 'handover_followup', followup_id,
          result=data.result, observation_id=observation_id)
    return result
