'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {test}=require('node:test');
function setup(){
  const nodes=new Map(),events={};let receive;
  for(const key of ['#document','#filename','#mode','#notice'])nodes.set(key,{textContent:'',innerHTML:'',hidden:false,classList:{toggle(){}},addEventListener(type,fn){this[type]=fn;}});
  const document={querySelector:key=>nodes.get(key),addEventListener:(key,fn)=>events[key]=fn};
  const context=vm.createContext({document,window:{chrome:{webview:{addEventListener:(key,fn)=>receive=fn}}}});
  for(const file of ['markdown-preview.js','quick-reader.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend',file),'utf8'),context);
  return {nodes,events,send:value=>receive({data:value})};
}
test('Markdown is escaped and formatted while file names remain text',()=>{
  const s=setup();s.send({name:'<img>.md',markdown:true,content:'# 标题\r\n**粗体**\r\n<script>alert(1)</script>\n![x](https://remote/a.png)'});
  assert.equal(s.nodes.get('#filename').textContent,'<img>.md');
  const html=s.nodes.get('#document').innerHTML;assert.match(html,/<h1>标题<\/h1>/);assert.match(html,/<strong>粗体<\/strong>/);assert.doesNotMatch(html,/<script|<img/);
});
test('source view retains complete large text including mixed line endings',()=>{
  const s=setup(),content='# 标题\r\n'+ '正文\n'.repeat(60000)+'最后\r一行';s.send({name:'large.md',markdown:true,content});
  assert.match(s.nodes.get('#notice').textContent,/14 万/);s.nodes.get('#mode').click();
  assert.equal(s.nodes.get('#document').textContent,content);assert.equal(s.nodes.get('#notice').textContent,'');s.nodes.get('#mode').click();assert.match(s.nodes.get('#document').innerHTML,/<h1>标题/);
});
test('plain text does not interpret Markdown or HTML',()=>{
  const s=setup(),content='# 原文\r\n<b>not html</b>\r\n';s.send({name:'文本.txt',markdown:false,content});assert.equal(s.nodes.get('#document').textContent,content);assert.equal(s.nodes.get('#mode').hidden,true);
});
test('malformed host messages leave the current document intact',()=>{
  const s=setup();s.send({name:'a.txt',markdown:false,content:'保留'});for(const value of [null,{}, {name:'evil',markdown:'true',content:'replace'}])s.send(value);assert.equal(s.nodes.get('#document').textContent,'保留');
});
test('links cannot navigate away from the reader',()=>{
  const s=setup();let cancelled=false;s.events.click({target:{closest:()=>({})},preventDefault:()=>cancelled=true});assert.equal(cancelled,true);
});
