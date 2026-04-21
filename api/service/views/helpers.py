"""Helpers facade for views.

This module re-exports helper functions/constants from legacy during
the transition. Domain modules should import from here instead of
importing helpers directly from legacy.
"""

from .helpers_impl import (
    # Auth/login helpers and constants
    TOKEN_TTL_MIN,
    COOLDOWN_MIN,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_LOCKOUT_SECONDS,
    PASSWORD_MIN_LENGTH,
    _login_rate_key,
    _is_login_locked,
    _register_login_failure,
    _reset_login_failure,
    _validate_password_strength,

    # DB helpers
    q,
    exec_void,
    exec_returning,
    last_insert_id,
    _set_audit_user,

    # Encoding/text helpers
    _fix_text_value,
    _fix_row,
    _norm_txt,

    # Motivo ENUM helpers
    _get_motivo_enum_values,
    _get_motivo_enum_values_raw,
    _map_motivo_to_db_label,
    _motivo_is_cotizacion_equipo,
    _fetchall_dicts,

    # Roles/auth helpers
    require_roles,
    require_roles_strict,
    require_jefe,
    require_permission,
    _rol,
    _is,
    _in,

    # Misc
    ensure_default_locations,
    money,
    _frontend_url,
    # Email footer helpers
    _load_email_footer_text,
    _email_append_footer_text,
    _email_append_footer_html,
    # Labels
    os_label,
    # Business-hours/metrics utils
    WORKDAY_START_HOUR,
    WORKDAY_END_HOUR,
    WORKDAYS,
    _tz_aware,
    _clamp_to_work_window_forward,
    _clamp_to_work_window_backward,
    _holidays_country,
    _parse_extra_holidays_env,
    _fetch_nager_year,
    _get_year_holidays,
    _holidays_between,
    business_minutes_between,
)

__all__ = [
    # constants
    'TOKEN_TTL_MIN','COOLDOWN_MIN','LOGIN_MAX_ATTEMPTS','LOGIN_LOCKOUT_MINUTES','LOGIN_LOCKOUT_SECONDS','PASSWORD_MIN_LENGTH',
    # auth/login throttling
    '_login_rate_key','_is_login_locked','_register_login_failure','_reset_login_failure','_validate_password_strength',
    # db
    'q','exec_void','exec_returning','last_insert_id','_set_audit_user',
    # text
    '_fix_text_value','_fix_row','_norm_txt',
    # motivo
    '_get_motivo_enum_values','_get_motivo_enum_values_raw','_map_motivo_to_db_label','_fetchall_dicts',
    '_motivo_is_cotizacion_equipo',
    # roles
    'require_roles','require_roles_strict','require_jefe','require_permission','_rol','_is','_in',
    # misc
    'ensure_default_locations','money','_frontend_url','os_label',
    '_load_email_footer_text','_email_append_footer_text','_email_append_footer_html',
    # business-hours/metrics utils (compat)
    'WORKDAY_START_HOUR','WORKDAY_END_HOUR','WORKDAYS',
    '_tz_aware','_clamp_to_work_window_forward','_clamp_to_work_window_backward',
    '_holidays_country','_parse_extra_holidays_env','_fetch_nager_year','_get_year_holidays',
    '_holidays_between','business_minutes_between',
]
