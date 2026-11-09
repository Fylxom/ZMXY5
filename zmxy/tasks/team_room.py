"""十绝阵、师徒试炼、斗神大会共用的在线副本步骤。这里不注册任务（导入它不影响任务顺序）。"""

from __future__ import annotations

from typing import Any

from .. import online, protocol as p, state
from ..core import UncertainOutcome
from ..online import R_ENTER, R_EXIT_ROOM, R_KEYS

READY_KEY = 84


def created_room_id(response: Any) -> str:
    """createWaitingRoom 响应里的房间号；没有就是空串。"""
    return str(state.find(response, "roomId", "room_id", depth=8) or "")


def leave_room(owner, room_id: str) -> None:
    """退等待房间。owner 是 ctx 或 Session；退不掉不追究。"""
    if room_id:
        try:
            owner.request(R_EXIT_ROOM, {"roomId": room_id})
        except (p.AllianceError, UncertainOutcome):
            pass


def enter_battle(team, where: online.Endpoint, player_id, cg: str) -> Any:
    return team.request(R_ENTER, {"instId": where.inst_id, "playerId": player_id, "password": where.password,
                                  "abnormalFrameNum": 0, "cg": cg})


def frame_step(entered: Any) -> int:
    """每条帧推送代表的帧数（enter 响应的 sendKeyFrameNum）。"""
    return max(1, state.num(state.find(entered, "sendKeyFrameNum", "send_key_frame_num", depth=4)) or 1)


def send_ready(team, slot: int) -> None:
    team.notify(R_KEYS, {"keys": [READY_KEY], "objs": [{"ready": True, "uid": slot, "teamPs": slot}]})
