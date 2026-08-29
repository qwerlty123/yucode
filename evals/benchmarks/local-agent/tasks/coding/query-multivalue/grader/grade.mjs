      import fs from 'node:fs';
      import path from 'node:path';
      import { pathToFileURL } from 'node:url';
      const output = process.env.YUCODE_EVAL_OUTPUT || '.';
      fs.mkdirSync(output, {recursive:true});
      let result;
      try {
        const mod = await import(pathToFileURL(path.resolve('query.mjs')).href + '?fixed=1');
        const check = (condition, message) => { if (!condition) throw new Error(message); };
      const value = mod.parseQuery('?tag=a&tag=b&empty=&q=hello+world');
check(Object.getPrototypeOf(value) === null, 'prototype');
check(JSON.stringify(value) === JSON.stringify({tag:['a','b'],empty:[''],q:['hello world']}), 'values');
let threw = false; try { mod.parseQuery('__proto__=x'); } catch { threw = true; } check(threw, 'unsafe key');
        result = {passed:true};
      } catch (error) { result = {passed:false,error:`${error.name}: ${error.message}`}; }
      fs.writeFileSync(path.join(output, 'grade.json'), JSON.stringify(result) + '\n');
      console.log(JSON.stringify(result));
      process.exit(result.passed ? 0 : 1);
