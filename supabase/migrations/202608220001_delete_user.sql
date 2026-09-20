-- Hard-delete an account. Super-admin only, irreversible.
--
-- Deleting auth.users cascades to public.user_profiles and public.app_sessions
-- (both declared "references auth.users(id) on delete cascade").
--
-- admin_audit_logs.target_user_id is "on delete set null", so the audit row
-- survives the delete but forgets who it was about. The identity is therefore
-- copied into before_state BEFORE the delete -- after that it is the only
-- remaining record of which account was removed.

begin;

create or replace function public.admin_delete_user(
    p_actor_user_id uuid,
    p_target_user_id uuid,
    p_reason text,
    p_ip_hash text,
    p_now timestamptz default now()
)
returns jsonb
language plpgsql
security definer
set search_path = public, auth
as $$
declare
    v_actor public.user_profiles%rowtype;
    v_target public.user_profiles%rowtype;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_active_supers integer := 0;
    v_before jsonb;
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

    -- Deleting the last super admin would lock everyone out of the admin
    -- surface, and would also be a back door around the same guard that
    -- admin_change_user_role already enforces for demotion.
    if v_target.role = 'super_admin' then
        select count(*) into v_active_supers
        from public.user_profiles
        where role = 'super_admin' and status = 'active';
        if v_active_supers <= 1 then
            raise exception 'last_super_admin_denied' using errcode = '42501';
        end if;
    end if;

    v_before := to_jsonb(v_target);

    -- Audit first: the delete below nulls this row's target_user_id.
    insert into public.admin_audit_logs (
        actor_user_id, action, target_user_id, reason,
        before_state, after_state, created_at, ip_hash
    ) values (
        p_actor_user_id, 'delete_user', p_target_user_id, v_reason,
        v_before, '{}'::jsonb, p_now, coalesce(p_ip_hash, '')
    );

    delete from auth.users where id = p_target_user_id;

    return v_before;
end;
$$;

revoke all on function public.admin_delete_user(
    uuid, uuid, text, text, timestamptz
) from public, anon, authenticated;

grant execute on function public.admin_delete_user(
    uuid, uuid, text, text, timestamptz
) to service_role;

commit;
