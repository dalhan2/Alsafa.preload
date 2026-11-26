"""Effect / transition / animation catalogue for the CapCut editor tools.

The data in catalogue.json is extracted from capcut-cli's enums.json (CapCut
namespace). It is a snapshot of the packages the installed CapCut build knows
by resource id; CapCut still resolves the actual media from its own cache.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(HERE, 'catalogue.json')
_CACHE = None


def catalogue():
    global _CACHE
    if _CACHE is None:
        try:
            with open(_PATH, encoding='utf-8') as f:
                _CACHE = json.load(f)
        except Exception:
            _CACHE = {}
    return _CACHE


def _norm(value):
    return str(value or '').strip().lower().replace('_', '-').replace(' ', '-')


def _match(entry, ident):
    wanted = _norm(ident)
    if not wanted:
        return False
    for key in ('slug', 'member', 'name', 'title', 'effect_id', 'resource_id',
                'resource_type'):
        value = entry.get(key)
        if value and _norm(value) == wanted:
            return True
    return False


def _find_in(groups, ident):
    for group in groups:
        for entry in catalogue().get(group) or []:
            if _match(entry, ident):
                found = dict(entry)
                found.setdefault('group', group)
                return found
    return None


def find_effect(ident):
    return _find_in(('scene_effects', 'character_effects', 'audio_effects'), ident)


def find_transition(ident):
    return _find_in(('transitions',), ident)


def find_mask(ident):
    return _find_in(('masks',), ident)


def find_animation(ident, slot='intro', material_type='video'):
    """Find an in/out/loop animation package.

    slot: 'intro', 'outro' or 'loop'. The catalogue has separate packages for
    image (video/sticker) and text; the material type picks the right family.
    """
    if material_type == 'text':
        groups = {'intro': ('text_intros',), 'outro': ('text_outros',),
                  'loop': ('text_loops',)}.get(slot, ('text_intros',))
    else:
        groups = {'intro': ('image_intros',), 'outro': ('image_outros',),
                  'loop': ('image_combos',)}.get(slot, ('image_intros',))
    found = _find_in(groups, ident)
    if found:
        return found
    # fall back to the other family if the slug is shared (fade-in etc.)
    other = ('text_intros', 'text_outros', 'text_loops') if material_type != 'text' else (
        'image_intros', 'image_outros', 'image_combos')
    return _find_in(other, ident)


def list_packages(kind=None):
    """Compact list for effects_list()."""
    c = catalogue()
    groups = {
        'effect': ('scene_effects', 'character_effects'),
        'filter': ('filters',),
        'audio': ('audio_effects',),
        'transition': ('transitions',),
        'generator': ('image_intros', 'image_outros', 'image_combos',
                      'text_intros', 'text_outros', 'text_loops'),
        'mask': ('masks',),
    }
    if kind and kind in groups:
        wanted = groups[kind]
    elif kind:
        wanted = ()
    else:
        wanted = tuple(groups['effect'] + groups['filter'] + groups['audio'] +
                       groups['transition'] + groups['generator'] + groups['mask'])
    out = []
    for group in wanted:
        for entry in c.get(group) or []:
            item = dict(entry)
            item['group'] = group
            item.setdefault('id', item.get('slug') or item.get('effect_id'))
            out.append(item)
    return out
