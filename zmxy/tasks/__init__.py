"""导入即注册。新增任务：在本目录加一个文件，并在下面加一行 import。

这里主要决定界面显示顺序；core.py 还会按 first/last/terminal 调整执行顺序。
"""

from . import (  # noqa: F401
    double_drop, online_welfare,
)
from .events import festival  # noqa: F401
from .events import hallowmas  # noqa: F401
from .events import double11_dog  # noqa: F401
from . import (  # noqa: F401
    fulushou, growth_road, pet_collection, fairy_trial, mystery_shop, black_market, pet_elf, shanfa,
    monster_card, wish_pool, fairy_feed, union_warehouse, arena, treasure_cave, fairy_flag_shop,
    quick_develop, pet_trial, daily_quest, special, wanxiang_tree, tree_king, maze_free, refine,
    teacher, dungeons, alliance, vip_gift, month_card, limited, cyclic_trial,
)
from .events import game_high_jump  # noqa: F401
from . import (  # noqa: F401
    chapter_role, login_gift, limited_quest, dragon_treasure, ancient, demon_tower, last_rewards,
    tree_king_nightmare, star_maze, time_boat, limited_shop, tongtian, tongtian_escort, ten_jue,
    teacher_trial, pet_brawl, pvp_match, pet_catch,
)
from .pet_cultivation import task  # noqa: F401
from . import (  # noqa: F401
    arena_apply, garden, boss_raid, uid_lookup, group_signin,
)
