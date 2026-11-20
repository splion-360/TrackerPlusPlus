// Build each persona's memory with Tony's memory agent (memory-agent/), and save the facts and
// planner text for the harness. No network, no model calls. Run from the repo root:
//   node harness/scripts/build_memory.mjs
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { openNodeStore } from '../../memory-agent/src/store/node/node-store.mjs';
import { rollDay } from '../../memory-agent/src/memory/roll.mjs';
import { plannerText } from '../../memory-agent/src/memory/planner-text.mjs';
import { loadPersona, userIdOf } from '../../memory-agent/scripts/lib/trial-core.mjs';

const MAP = { sam: 'sedentary', alex: 'moderate', jordan: 'half_marathon' };
const out = path.resolve('harness/harness/data/memory');
for (const [persona, who] of Object.entries(MAP)) {
  const snapshot = loadPersona(who);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tpp-mem-'));
  try {
    const store = openNodeStore(root, userIdOf(who));
    const lastDay = snapshot.dailySummaries.at(-1).date;
    const day = new Date(Date.parse(`${lastDay}T00:00:00Z`) + 86400000).toISOString().slice(0, 10);
    const now = `${day}T00:00:00-08:00`;
    rollDay({ store, snapshot: { ...snapshot, exportDate: now }, now });
    const facts = ['profile', 'rolling', 'preferences']
      .flatMap((name) => (store.read(name)?.facts ?? []).map((f) => ({ file: name, ...f })))
      .filter((f) => f.kind !== 'daily' || f.key === lastDay);
    const planner = plannerText(store);
    fs.writeFileSync(path.join(out, `${persona}.json`), JSON.stringify({ persona, asOf: now, facts, plannerText: planner.text, words: planner.words }, null, 1));
    console.log(`${persona}: ${facts.length} facts, planner ${planner.words} words`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}
