import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const source = path.join(root, 'prompts');
const document = JSON.parse(fs.readFileSync(path.join(source, 'players.json'), 'utf8'));
const pressing = fs.readFileSync(path.join(source, 'pressing.txt'), 'utf8').trim();
const coordinates = fs.readFileSync(path.join(source, 'coordinates.txt'), 'utf8').trim();
const outfieldCoordinates = fs.readFileSync(path.join(source, 'outfield_coordinates.txt'), 'utf8').trim();
assert.deepEqual(document.players.map(p => p.player_id), [0, 1, 2, 3, 4]);
assert.equal(new Set(document.players.map(p => p.name)).size, 5);
for (const player of document.players) {
  assert.match(player.name, /^[A-Za-z][A-Za-z0-9_]*$/);
  const template = player.template ?? 'goalkeeper.txt';
  assert.match(template, /^[A-Za-z0-9_.-]+$/);
  const values = { player_name: player.name.replaceAll('_', ' '),
    player_id: player.player_id, aim_location: player.aim_location, coordinate_instruction: coordinates,
    native_coordinate_instruction: outfieldCoordinates,
    example_receiver: player.player_id === 4 ? 3 : 4,
    role_instruction: player.player_id === 0 ? '' : fs.readFileSync(path.join(source, 'roles', `${player.role}.txt`), 'utf8').trim(), pressing_instruction: pressing };
  const text = fs.readFileSync(path.join(source, template), 'utf8')
    .replace(/\$([a-z_]+)/g, (_, key) => {
      assert.notEqual(values[key], undefined, `Unknown variable ${key}`);
      return values[key];
    });
  assert(text.length <= 20000, `${player.name}: instructions exceed console limit`);
  assert(!/\$[a-z_]+/.test(text), `${player.name}: unresolved variable`);
  for (const line of text.split(/\r?\n/).filter(line => line.startsWith('[{'))) {
    const commands = JSON.parse(line);
    assert.equal(commands.length, 1);
    assert.equal(commands[0].playerId, player.player_id);
    assert([0, 1].includes(commands[0].teamId));
    assert(Number.isInteger(commands[0].duration));
    if (commands[0].commandType === 'SHOOT') {
      assert(['BL', 'BR', 'CENTER'].includes(commands[0].parameters.aim_location));
      assert(commands[0].parameters.power >= 0 && commands[0].parameters.power <= 1);
    }
  }
  const destination = path.join(root, `${player.name}.txt`);
  if (process.argv.includes('--write')) fs.writeFileSync(destination, text);
  else assert.equal(fs.readFileSync(destination, 'utf8'), text, `${player.name}: stale snapshot`);
  console.log(`${player.name}: OK (${text.length} characters)`);
}
