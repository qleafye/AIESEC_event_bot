"""Хвост рассылки: соседние швы регистрируют хендлеры на общий admin.router.

Их тянет хвост `admin_broadcasts.py` одной строкой. Логика остаётся в своих файлах —
иначе `admin_broadcasts.py` упирается в потолок размера.
"""
from handlers.comms import admin_broadcast_session_filter  # noqa: F401
from handlers.comms import admin_broadcast_ext_form_filter  # noqa: F401
from handlers.comms import admin_broadcast_season  # noqa: F401
from handlers.comms import admin_broadcast_enroll_filter  # noqa: F401
from handlers.comms import admin_broadcast_status  # noqa: F401
from handlers.comms import admin_broadcast_interview  # noqa: F401
from handlers.comms import admin_broadcast_consent  # noqa: F401
from handlers.comms import admin_broadcast_vk  # noqa: F401
