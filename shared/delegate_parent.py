"""Pure parent ranking and cycle-safe ancestry for the delegate wire object."""


def resolve_parent(session, open_sessions, recorded, prefixes=('del_',)):
    names = set(open_sessions)
    def delegate(name):
        return any(name.startswith(p) for p in prefixes)
    def direct(name):
        prefix = next((p for p in prefixes if name.startswith(p)), None)
        rest = name[len(prefix):] if prefix else ''
        origin, cross, target = rest.partition('-to-')
        base = dict(parent_session=None, parent_source='name', match='unmatched',
                    candidates=[], target_repo=target.split('-')[0] if cross else None)
        if name in recorded and recorded[name]:
            parent = recorded[name]
            base.update(parent_session=parent, parent_source='recorded',
                        match='attached' if parent in names else 'unmatched',
                        candidates=[parent] if parent in names else [])
            return base
        if not prefix or not origin or (cross and not target):
            base['match'] = 'invalid'
            return base
        keys = {}
        for candidate in sorted(names):
            if not delegate(candidate):
                key = candidate.removeprefix('drift-further_').casefold()
                keys.setdefault(key, []).append(candidate)
        segments = origin.casefold().split('-')
        lengths = range(len(segments) if cross else len(segments) - 1, 0, -1)
        prefixes_ranked = ['-'.join(segments[:n]) for n in lengths]
        matches = []
        for key in prefixes_ranked:
            if key in keys:
                matches = keys[key]
                break
        if not matches:
            for key in prefixes_ranked:
                matches = [candidate for k, candidates in keys.items() if k.startswith(key + '-')
                           for candidate in candidates]
                if matches:
                    break
        if matches:
            base.update(candidates=sorted(matches), match='attached' if len(matches) == 1 else 'ambiguous',
                        parent_session=matches[0] if len(matches) == 1 else None)
        return base
    result = direct(session)
    current = session
    seen = {session}
    hop = result
    while hop['match'] == 'attached':
        current = hop['parent_session']
        if current in seen:
            result['match'] = 'cycle'
            return result
        seen.add(current)
        if not delegate(current):
            result['ancestor_session'] = current
            return result
        hop = direct(current)
    if result['match'] == 'attached':
        # The last open delegate is itself an orphan, but still owns its children.
        result['ancestor_session'] = current
    return result


def enrich_parents(panes, prefixes=('del_',)):
    """Use metadata from the same inventory, never a second stale listing."""
    names = {p['session'] for p in panes}
    recorded = {p['session']: p.get('_assist_parent') for p in panes if p.get('_assist_parent')}
    resolved = {name: resolve_parent(name, names, recorded, prefixes)
                for name in names if any(name.startswith(p) for p in prefixes)}
    for pane in panes:
        pane.pop('_assist_parent', None)
        if pane['session'] in resolved:
            pane['delegate'] = {**(pane.get('delegate') or {}), **resolved[pane['session']]}
