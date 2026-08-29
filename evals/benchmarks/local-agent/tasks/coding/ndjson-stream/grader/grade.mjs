      import fs from 'node:fs';
      import path from 'node:path';
      import { pathToFileURL } from 'node:url';
      const output = process.env.YUCODE_EVAL_OUTPUT || '.';
      fs.mkdirSync(output, {recursive:true});
      let result;
      try {
        const mod = await import(pathToFileURL(path.resolve('ndjson.mjs')).href + '?fixed=1');
        const check = (condition, message) => { if (!condition) throw new Error(message); };
      check(JSON.stringify(mod.parseNDJSON(['{"a":', '1}\n\n{"b":2', '}'])) === '[{"a":1},{"b":2}]', 'chunks');
let message = ''; try { mod.parseNDJSON(['{"ok":1}\n', '{bad}']); } catch (error) { message = error.message; }
check(message.includes('line 2'), message);
        result = {passed:true};
      } catch (error) { result = {passed:false,error:`${error.name}: ${error.message}`}; }
      fs.writeFileSync(path.join(output, 'grade.json'), JSON.stringify(result) + '\n');
      console.log(JSON.stringify(result));
      process.exit(result.passed ? 0 : 1);
