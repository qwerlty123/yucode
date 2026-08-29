      import fs from 'node:fs';
      import path from 'node:path';
      import { pathToFileURL } from 'node:url';
      const output = process.env.YUCODE_EVAL_OUTPUT || '.';
      fs.mkdirSync(output, {recursive:true});
      let result;
      try {
        const mod = await import(pathToFileURL(path.resolve('task_pool.mjs')).href + '?fixed=1');
        const check = (condition, message) => { if (!condition) throw new Error(message); };
      const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
let active = 0, peak = 0; const started = [], release = [];
const tasks = [0, 1, 2, 3].map(index => () => {
  active++; peak=Math.max(peak,active); started.push(index);
  return new Promise(resolve => { release[index] = () => { active--; resolve(index); }; });
});
const pending = mod.runPool(tasks, 2); await flush();
check(JSON.stringify(started) === '[0,1]', `initial ${started}`); check(peak === 2, `peak ${peak}`);
release[1](); await flush(); check(JSON.stringify(started) === '[0,1,2]', `third ${started}`);
release[0](); await flush(); check(JSON.stringify(started) === '[0,1,2,3]', `fourth ${started}`);
release[2](); release[3](); check(JSON.stringify(await pending) === '[0,1,2,3]', 'order');
const failureStarted = []; let rejectFirst;
const failed = mod.runPool([
  () => { failureStarted.push(0); return new Promise((_resolve, reject) => { rejectFirst = reject; }); },
  () => { failureStarted.push(1); return new Promise(() => {}); },
  () => { failureStarted.push(2); return Promise.resolve(2); },
], 2);
await flush(); rejectFirst(new Error('expected')); let rejected=false; try { await failed; } catch { rejected=true; }
await flush(); check(rejected && JSON.stringify(failureStarted) === '[0,1]', `failure ${failureStarted}`);
let threw=false; try { await mod.runPool([], 0); } catch { threw=true; } check(threw, 'limit');
        result = {passed:true};
      } catch (error) { result = {passed:false,error:`${error.name}: ${error.message}`}; }
      fs.writeFileSync(path.join(output, 'grade.json'), JSON.stringify(result) + '\n');
      console.log(JSON.stringify(result));
      process.exit(result.passed ? 0 : 1);
