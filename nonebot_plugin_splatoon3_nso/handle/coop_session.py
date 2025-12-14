from __future__ import annotations

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as dt, timedelta
from typing import Dict, List, Optional

from nonebot import on_command
from nonebot.params import Depends

from .send_msg import bot_send
from .utils import _check_session_handler
from ..data.data_source import dict_get_or_set_user_info
from ..s3s.splatoon import Splatoon
from ..utils import get_msg_id
from ..utils.bot import Bot, Event, logger


@dataclass
class CoopGameRecord:
    job_id: str
    played_time: dt
    result_wave: int
    job_point: int
    stage: str
    weapons: str
    grade_after: str
    grade_point_after: int

    @property
    def cleared(self) -> bool:
        return self.result_wave == 0

    @property
    def wave_text(self) -> str:
        return "EX" if self.result_wave == -1 else str(self.result_wave or 0)


@dataclass
class CoopSession:
    start_time: dt
    end_time: dt
    initial_grade: str
    initial_point: int
    group_key: str
    records: List[CoopGameRecord] = field(default_factory=list)

    def has_job(self, job_id: str) -> bool:
        return any(r.job_id == job_id for r in self.records)

    @property
    def current_grade(self) -> str:
        return self.records[-1].grade_after if self.records else self.initial_grade

    @property
    def current_point(self) -> int:
        return self.records[-1].grade_point_after if self.records else self.initial_point

    @property
    def start_text(self) -> str:
        return self.start_time.strftime("%m-%d %H:%M")

    @property
    def end_text(self) -> str:
        return self.end_time.strftime("%m-%d %H:%M")

    def summary_rows(self) -> List[str]:
        rows = []
        for idx, record in enumerate(sorted(self.records, key=lambda r: r.played_time)):
            result_text = "Clear" if record.cleared else f"W{record.wave_text} Fail"
            point_text = f"{record.job_point:+d}" if record.job_point else "0"
            rows.append(
                f"|{idx + 1}|{record.played_time.strftime('%m-%d %H:%M')}|{result_text}|"
                f"{record.wave_text}|{point_text}|{record.stage}|{record.weapons}|"
            )
        return rows


sessions: Dict[str, CoopSession] = {}

matcher_coop_session = on_command("coop_session", aliases={"打工统计", "coopstat"}, priority=10, block=True)


@matcher_coop_session.handle(parameterless=[Depends(_check_session_handler)])
async def _(bot: Bot, event: Event):
    platform = bot.adapter.get_name()
    user_id = event.get_user_id()
    msg_id = get_msg_id(platform, user_id)
    user = dict_get_or_set_user_info(platform, user_id)
    splatoon = Splatoon(bot, event, user)

    try:
        coop_res = await splatoon.get_coops(multiple=True)
        coop_data = coop_res["data"]["coopResult"]
        session = await build_or_update_session(msg_id, coop_data, splatoon)
        await bot_send(bot, event, message=session_to_md(session))
    except Exception as e:
        logger.warning(f"coop session error: {e}")
        await bot_send(bot, event, "获取打工统计失败，请稍后重试")


async def build_or_update_session(msg_id: str, coop_data: dict, splatoon: Splatoon) -> CoopSession:
    group = (coop_data.get("historyGroups") or {}).get("nodes") or []
    if not group:
        raise ValueError("no coop history")

    current_group = group[0]
    start_time = parse_iso_time(current_group.get("startTime"))
    end_time = parse_iso_time(current_group.get("endTime"))
    initial_grade = (coop_data.get("regularGrade") or {}).get("name") or ""
    initial_point = coop_data.get("regularGradePoint") or 0
    group_key = f"{current_group.get('startTime')}|{current_group.get('endTime')}"

    session = sessions.get(msg_id)
    if not session or session.group_key != group_key:
        session = CoopSession(
            start_time=start_time,
            end_time=end_time,
            initial_grade=initial_grade,
            initial_point=initial_point,
            group_key=group_key,
        )
        sessions[msg_id] = session

    detail_nodes = (current_group.get("historyDetails") or {}).get("nodes") or []
    for node in detail_nodes:
        job_id = node.get("id")
        if not job_id or session.has_job(job_id):
            continue
        detail = await splatoon.get_coop_detail(job_id, multiple=True)
        detail_data = (detail.get("data") or {}).get("coopHistoryDetail") or {}
        record = parse_detail(detail_data, initial_grade, initial_point)
        if record:
            session.records.append(record)

    return session


def parse_detail(detail: dict, default_grade: str, default_point: int) -> Optional[CoopGameRecord]:
    try:
        played_time = parse_iso_time(detail.get("playedTime"))
        stage = (detail.get("coopStage") or {}).get("name") or ""
        result_wave = detail.get("resultWave")
        job_point = detail.get("jobPoint") or 0
        grade_after = (detail.get("afterGrade") or {}).get("name") or default_grade
        grade_point_after = detail.get("afterGradePoint") or default_point
        weapons = parse_weapons(detail)
        job_id = detail.get("id") or detail.get("judgement") or played_time.strftime("%Y%m%d%H%M%S")
    except Exception as e:
        logger.warning(f"parse coop detail failed: {e}")
        return None

    return CoopGameRecord(
        job_id=str(job_id),
        played_time=played_time,
        result_wave=int(result_wave) if result_wave is not None else -1,
        job_point=int(job_point),
        stage=stage,
        weapons=weapons,
        grade_after=grade_after,
        grade_point_after=int(grade_point_after),
    )


def parse_weapons(detail: dict) -> str:
    my_result = detail.get("myResult") or {}
    weapons = my_result.get("weapons") or []
    names = [w.get("name") for w in weapons if w.get("name")]
    return ", ".join(names) if names else "--"


def parse_iso_time(raw: Optional[str]) -> dt:
    if not raw:
        return dt.now()
    return dt.strptime(raw, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)


def session_to_md(session: CoopSession) -> str:
    header = [
        "#### 鲑鱼跑自动统计",
        f"##### 本期：{session.start_text} ~ {session.end_text}",
        f"当前段位：{session.current_grade} {session.current_point} (初始 {session.initial_grade} {session.initial_point})",
        f"已打局数：{len(session.records)}",
        "",
        "|场次|时间|结果|波数|点数|地图|武器|",
        "|:--:|:--|:--|:--:|--:|--|--|",
    ]
    header.extend(session.summary_rows() or ["| - | - | - | - | - | - | - |"])
    return "\n".join(header)
