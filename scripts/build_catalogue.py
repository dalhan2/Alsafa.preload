"""Build catalogue.json from capcut-cli's extracted enums.

Source: https://github.com/renezander030/capcut-cli/blob/master/src/enums.json
Run: python scripts/build_catalogue.py path/to/enums.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, 'catalogue.json')


def compact(items, kind, *fields):
    out = []
    for item in items or []:
        entry = {'kind': kind}
        for field in fields:
            if field in item:
                entry[field] = item[field]
        # a couple of aliases make the MCP surface friendlier
        if 'title' in entry and 'name' not in entry:
            entry['name'] = entry['title']
        out.append(entry)
    return out


def main(src):
    raw = json.load(open(src, encoding='utf-8'))
    cap = raw['capcut']
    catalogue = {
        'source': 'capcut-cli enums.json (CapCut namespace)',
        'transitions': compact(cap.get('transitions'), 'transition',
                               'slug', 'name', 'default_duration', 'effect_id',
                               'resource_id', 'is_overlap', 'is_vip'),
        'masks': compact(cap.get('masks'), 'mask',
                         'slug', 'name', 'default_aspect_ratio', 'effect_id',
                         'resource_id', 'resource_type', 'is_vip'),
        'image_intros': compact(cap.get('image_intros'), 'image_intro',
                                'slug', 'title', 'duration', 'effect_id',
                                'resource_id', 'is_vip'),
        'image_outros': compact(cap.get('image_outros'), 'image_outro',
                                'slug', 'title', 'duration', 'effect_id',
                                'resource_id', 'is_vip'),
        'image_combos': compact(cap.get('image_combos'), 'image_loop',
                                'slug', 'title', 'duration', 'effect_id',
                                'resource_id', 'is_vip'),
        'text_intros': compact(cap.get('text_intros'), 'text_intro',
                               'slug', 'title', 'duration', 'effect_id',
                               'resource_id', 'is_vip'),
        'text_outros': compact(cap.get('text_outros'), 'text_outro',
                               'slug', 'title', 'duration', 'effect_id',
                               'resource_id', 'is_vip'),
        'text_loops': compact(cap.get('text_loop_anims'), 'text_loop',
                              'slug', 'title', 'duration', 'effect_id',
                              'resource_id', 'is_vip'),
        'scene_effects': compact(cap.get('scene_effects'), 'effect',
                                 'slug', 'name', 'effect_id', 'resource_id',
                                 'is_vip'),
        'character_effects': compact(cap.get('character_effects'), 'effect',
                                     'slug', 'name', 'effect_id', 'resource_id',
                                     'is_vip'),
        'audio_effects': compact(cap.get('audio_effects'), 'audio',
                                 'slug', 'name', 'effect_id', 'resource_id',
                                 'is_vip'),
        'filters': [],
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(catalogue, f, ensure_ascii=False, indent=1)
    counts = {k: len(v) for k, v in catalogue.items() if isinstance(v, list)}
    print('wrote', OUT, counts)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else sys.argv[0])
