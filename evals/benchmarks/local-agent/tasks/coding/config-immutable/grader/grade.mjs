      import fs from 'node:fs';
      import path from 'node:path';
      import { pathToFileURL } from 'node:url';
      const output = process.env.YUCODE_EVAL_OUTPUT || '.';
      fs.mkdirSync(output, {recursive:true});
      let result;
      try {
        const mod = await import(pathToFileURL(path.resolve('config.mjs')).href + '?fixed=1');
        const check = (condition, message) => { if (!condition) throw new Error(message); };
      const base={http:{port:80,headers:{a:'1'}},list:[1]}; const over={http:{headers:{b:'2'}},list:[2,3]};
const before=JSON.stringify([base,over]); const got=mod.mergeConfig(base,over);
check(JSON.stringify(got)==='{"http":{"port":80,"headers":{"a":"1","b":"2"}},"list":[2,3]}','merge');
got.http.headers.b='x'; check(over.http.headers.b==='2','alias'); check(JSON.stringify([base,over])===before,'mutation');
const polluted=JSON.parse('{"__proto__":{"polluted":true},"safe":1}'); check(mod.mergeConfig({},polluted).safe===1 && ({}).polluted===undefined,'pollution');
        result = {passed:true};
      } catch (error) { result = {passed:false,error:`${error.name}: ${error.message}`}; }
      fs.writeFileSync(path.join(output, 'grade.json'), JSON.stringify(result) + '\n');
      console.log(JSON.stringify(result));
      process.exit(result.passed ? 0 : 1);
