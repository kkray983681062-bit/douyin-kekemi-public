begin;

create table if not exists public.user_profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    username text not null,
    username_normalized text not null unique,
    email_normalized text not null unique,
    role text not null default 'member'
        check (role in ('member', 'admin', 'super_admin')),
    status text not null default 'pending'
        check (status in ('pending', 'active', 'rejected', 'suspended')),
    status_reason text not null default '',
    approved_at timestamptz,
    approved_by uuid references auth.users(id) on delete set null,
    suspended_at timestamptz,
    suspended_by uuid references auth.users(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists user_profiles_status_created_idx
    on public.user_profiles(status, created_at desc);

create table if not exists public.app_sessions (
    id uuid primary key default gen_random_uuid(),
    token_hash text not null unique,
    user_id uuid not null references auth.users(id) on delete cascade,
    access_level text not null check (access_level in ('status', 'app')),
    csrf_hash text not null,
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked_at timestamptz,
    created_ip_hash text not null default '',
    user_agent text not null default ''
);

create index if not exists app_sessions_token_hash_idx
    on public.app_sessions(token_hash);
create index if not exists app_sessions_user_active_idx
    on public.app_sessions(user_id, expires_at desc)
    where revoked_at is null;

create table if not exists public.admin_audit_logs (
    id bigint generated always as identity primary key,
    actor_user_id uuid references auth.users(id) on delete set null,
    action text not null,
    target_user_id uuid references auth.users(id) on delete set null,
    reason text not null default '',
    before_state jsonb not null default '{}'::jsonb,
    after_state jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    ip_hash text not null default ''
);

create index if not exists admin_audit_target_created_idx
    on public.admin_audit_logs(target_user_id, created_at desc);
create index if not exists admin_audit_created_idx
    on public.admin_audit_logs(created_at desc);

alter table public.user_profiles enable row level security;
alter table public.app_sessions enable row level security;
alter table public.admin_audit_logs enable row level security;

revoke all on table public.user_profiles from public, anon, authenticated;
revoke all on table public.app_sessions from public, anon, authenticated;
revoke all on table public.admin_audit_logs from public, anon, authenticated;
revoke all on sequence public.admin_audit_logs_id_seq from public, anon, authenticated;

grant all on table public.user_profiles to service_role;
grant all on table public.app_sessions to service_role;
grant all on table public.admin_audit_logs to service_role;
grant usage, select on sequence public.admin_audit_logs_id_seq to service_role;

create or replace function public.admin_transition_user(
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_action text,
    p_expected_status text,
    p_reason text,
    p_ip_hash text,
    p_now timestamptz default now()
)
returns public.user_profiles
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_actor public.user_profiles%rowtype;
    v_target public.user_profiles%rowtype;
    v_after public.user_profiles%rowtype;
    v_new_status text;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_active_super_admins integer;
begin
    select * into v_actor
    from public.user_profiles
    where id = p_actor_user_id
    for update;

    select * into v_target
    from public.user_profiles
    where id = p_target_user_id
    for update;

    if v_actor.id is null or v_actor.status <> 'active'
       or v_actor.role not in ('admin', 'super_admin') then
        raise exception 'permission_denied' using errcode = '42501';
    end if;
    if v_target.id is null then
        raise exception 'target_not_found' using errcode = 'P0002';
    end if;
    if v_actor.id = v_target.id then
        raise exception 'self_management_denied' using errcode = '42501';
    end if;
    if v_actor.role = 'admin' and v_target.role <> 'member' then
        raise exception 'permission_denied' using errcode = '42501';
    end if;
    if v_target.status <> p_expected_status then
        raise exception 'state_conflict' using errcode = '40001';
    end if;

    case p_action
        when 'approve' then
            if p_expected_status <> 'pending' then
                raise exception 'state_conflict' using errcode = '40001';
            end if;
            v_new_status := 'active';
        when 'reject' then
            if p_expected_status <> 'pending' or v_reason = '' then
                raise exception 'reason_required_or_state_conflict' using errcode = '22023';
            end if;
            v_new_status := 'rejected';
        when 'suspend' then
            if p_expected_status <> 'active' or v_reason = '' then
                raise exception 'reason_required_or_state_conflict' using errcode = '22023';
            end if;
            if v_target.role = 'super_admin' then
                select count(*) into v_active_super_admins
                from public.user_profiles
                where role = 'super_admin' and status = 'active';
                if v_active_super_admins <= 1 then
                    raise exception 'last_super_admin' using errcode = '42501';
                end if;
            end if;
            v_new_status := 'suspended';
        when 'restore' then
            if p_expected_status <> 'suspended' or v_reason = '' then
                raise exception 'reason_required_or_state_conflict' using errcode = '22023';
            end if;
            v_new_status := 'active';
        else
            raise exception 'invalid_action' using errcode = '22023';
    end case;

    update public.user_profiles
    set status = v_new_status,
        status_reason = case
            when p_action in ('reject', 'suspend') then v_reason
            else ''
        end,
        approved_at = case
            when p_action = 'approve' then p_now
            else approved_at
        end,
        approved_by = case
            when p_action = 'approve' then p_actor_user_id
            else approved_by
        end,
        suspended_at = case
            when p_action = 'suspend' then p_now
            when p_action = 'restore' then null
            else suspended_at
        end,
        suspended_by = case
            when p_action = 'suspend' then p_actor_user_id
            when p_action = 'restore' then null
            else suspended_by
        end,
        updated_at = p_now
    where id = p_target_user_id
    returning * into v_after;

    if p_action = 'suspend' then
        update public.app_sessions
        set revoked_at = p_now
        where user_id = p_target_user_id and revoked_at is null;
    end if;

    insert into public.admin_audit_logs (
        actor_user_id, action, target_user_id, reason,
        before_state, after_state, created_at, ip_hash
    ) values (
        p_actor_user_id, p_action, p_target_user_id, v_reason,
        to_jsonb(v_target), to_jsonb(v_after), p_now, coalesce(p_ip_hash, '')
    );

    return v_after;
end;
$$;

create or replace function public.admin_change_user_role(
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_role text,
    p_reason text,
    p_ip_hash text,
    p_now timestamptz default now()
)
returns public.user_profiles
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_actor public.user_profiles%rowtype;
    v_target public.user_profiles%rowtype;
    v_after public.user_profiles%rowtype;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_active_super_admins integer;
begin
    select * into v_actor
    from public.user_profiles
    where id = p_actor_user_id
    for update;

    select * into v_target
    from public.user_profiles
    where id = p_target_user_id
    for update;

    if v_actor.id is null or v_actor.status <> 'active'
       or v_actor.role <> 'super_admin' then
        raise exception 'permission_denied' using errcode = '42501';
    end if;
    if v_target.id is null then
        raise exception 'target_not_found' using errcode = 'P0002';
    end if;
    if v_actor.id = v_target.id then
        raise exception 'self_management_denied' using errcode = '42501';
    end if;
    if p_role not in ('member', 'admin') or v_reason = '' then
        raise exception 'invalid_role_or_reason' using errcode = '22023';
    end if;
    if v_target.role = 'super_admin' then
        select count(*) into v_active_super_admins
        from public.user_profiles
        where role = 'super_admin' and status = 'active';
        if v_active_super_admins <= 1 then
            raise exception 'last_super_admin' using errcode = '42501';
        end if;
    end if;

    update public.user_profiles
    set role = p_role,
        updated_at = p_now
    where id = p_target_user_id
    returning * into v_after;

    update public.app_sessions
    set revoked_at = p_now
    where user_id = p_target_user_id and revoked_at is null;

    insert into public.admin_audit_logs (
        actor_user_id, action, target_user_id, reason,
        before_state, after_state, created_at, ip_hash
    ) values (
        p_actor_user_id, 'role_change', p_target_user_id, v_reason,
        to_jsonb(v_target), to_jsonb(v_after), p_now, coalesce(p_ip_hash, '')
    );

    return v_after;
end;
$$;

create or replace function public.admin_force_revoke_sessions(
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_reason text,
    p_ip_hash text,
    p_now timestamptz default now()
)
returns integer
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_actor public.user_profiles%rowtype;
    v_target public.user_profiles%rowtype;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_revoked integer := 0;
begin
    select * into v_actor
    from public.user_profiles
    where id = p_actor_user_id
    for update;

    select * into v_target
    from public.user_profiles
    where id = p_target_user_id
    for update;

    if v_actor.id is null or v_actor.status <> 'active'
       or v_actor.role <> 'super_admin' then
        raise exception 'permission_denied' using errcode = '42501';
    end if;
    if v_target.id is null then
        raise exception 'target_not_found' using errcode = 'P0002';
    end if;
    if v_actor.id = v_target.id then
        raise exception 'self_management_denied' using errcode = '42501';
    end if;
    if v_reason = '' then
        raise exception 'reason_required' using errcode = '22023';
    end if;

    update public.app_sessions
    set revoked_at = p_now
    where user_id = p_target_user_id and revoked_at is null;
    get diagnostics v_revoked = row_count;

    insert into public.admin_audit_logs (
        actor_user_id, action, target_user_id, reason,
        before_state, after_state, created_at, ip_hash
    ) values (
        p_actor_user_id, 'force_revoke_sessions', p_target_user_id, v_reason,
        to_jsonb(v_target),
        to_jsonb(v_target) || jsonb_build_object('revoked_sessions', v_revoked),
        p_now, coalesce(p_ip_hash, '')
    );

    return v_revoked;
end;
$$;

create or replace function public.bootstrap_promote_super_admin(
    p_target_user_id uuid,
    p_reason text,
    p_now timestamptz default now()
)
returns public.user_profiles
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_target public.user_profiles%rowtype;
    v_after public.user_profiles%rowtype;
    v_reason text := btrim(coalesce(p_reason, ''));
begin
    select * into v_target
    from public.user_profiles
    where id = p_target_user_id
    for update;

    if v_target.id is null then
        raise exception 'target_not_found' using errcode = 'P0002';
    end if;
    if v_reason = '' then
        raise exception 'reason_required' using errcode = '22023';
    end if;

    update public.user_profiles
    set role = 'super_admin',
        status = 'active',
        status_reason = '',
        approved_at = coalesce(approved_at, p_now),
        suspended_at = null,
        suspended_by = null,
        updated_at = p_now
    where id = p_target_user_id
    returning * into v_after;

    update public.app_sessions
    set revoked_at = p_now
    where user_id = p_target_user_id and revoked_at is null;

    insert into public.admin_audit_logs (
        actor_user_id, action, target_user_id, reason,
        before_state, after_state, created_at, ip_hash
    ) values (
        null, 'bootstrap_super_admin', p_target_user_id, v_reason,
        to_jsonb(v_target), to_jsonb(v_after), p_now, ''
    );

    return v_after;
end;
$$;

revoke all on function public.admin_transition_user(
    uuid, uuid, text, text, text, text, timestamptz
) from public, anon, authenticated;
revoke all on function public.admin_change_user_role(
    uuid, uuid, text, text, text, timestamptz
) from public, anon, authenticated;
revoke all on function public.admin_force_revoke_sessions(
    uuid, uuid, text, text, timestamptz
) from public, anon, authenticated;
revoke all on function public.bootstrap_promote_super_admin(
    uuid, text, timestamptz
) from public, anon, authenticated;

grant execute on function public.admin_transition_user(
    uuid, uuid, text, text, text, text, timestamptz
) to service_role;
grant execute on function public.admin_change_user_role(
    uuid, uuid, text, text, text, timestamptz
) to service_role;
grant execute on function public.admin_force_revoke_sessions(
    uuid, uuid, text, text, timestamptz
) to service_role;
grant execute on function public.bootstrap_promote_super_admin(
    uuid, text, timestamptz
) to service_role;

commit;
