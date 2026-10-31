"""Small same-origin confirmation UI, independent of Grafana's action renderer."""
import html
import urllib.parse

STYLE='''body{margin:0;background:#101216;color:#e1e6ef;font:15px system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}main{width:min(620px,88vw);padding:30px;background:#191d24;border:1px solid #343b48;border-radius:12px;box-shadow:0 18px 60px #0008}h1{font-size:23px;margin:20px 0}p{line-height:1.65}dt{color:#a9b5c7;margin-top:16px}dd{margin:5px 0;overflow-wrap:anywhere}code{font-size:12px}.badge{color:#73d29e}.warn{color:#ffd08c}button,a.button{display:inline-block;border:0;border-radius:6px;padding:11px 18px;font:inherit;text-decoration:none;cursor:pointer}button{background:#bf3338;color:white}button:disabled{opacity:.55;cursor:wait}a{color:#8abfff}a.button{background:#303744;color:#e1e6ef;margin-left:10px}#result{white-space:pre-wrap}.error{color:#ff9b9b}'''


def page(title, content):
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>'+STYLE+'</style></head><body><main role="dialog" aria-modal="true">'+content+'</main><script src="/a5-control/confirm.js" defer></script></body></html>'


def notice(message, login_url, back):
    return page('容器操作', '<h1>容器操作</h1><p>'+html.escape(message)+'</p><a class="button" href="'+html.escape(login_url,quote=True)+'">管理员登录</a><a class="button" href="'+html.escape(back,quote=True)+'">返回面板</a>')


def confirmation(user, data, info, back):
    esc=lambda value:html.escape(str(value),quote=True)
    body='<div class="badge">已登录：'+esc(user['login'])+'（管理员）</div><h1>确认停止容器？</h1>'
    body+='<dl><dt>主机</dt><dd>'+esc(info['node'])+'</dd><dt>容器</dt><dd>'+esc(info['container'])+'</dd><dt>容器 ID</dt><dd><code>'+esc(info['container_id'])+'</code></dd></dl>'
    body+='<p class="warn">这会停止整个容器，容器内所有任务都会中断。先等待最多 30 秒正常退出，然后由 Docker 按停止规则处理。</p>'
    target=data['container_id']+'@'+data['container_started_at']
    body+='<form id="stop-form" data-node="'+esc(data['node'])+'" data-target="'+esc(target)+'"><button type="submit" id="stop-button">确认停止</button><a class="button" href="'+esc(back)+'">取消 / 返回面板</a></form><p id="result" role="status"></p>'
    return page('确认停止容器',body)


CONFIRM_JS='''"use strict";
const form=document.getElementById("stop-form");
if(form){form.addEventListener("submit",async(event)=>{event.preventDefault();
const button=document.getElementById("stop-button"),result=document.getElementById("result");
if(button.disabled)return;button.disabled=true;result.className="";result.textContent="正在停止，请等待…";
try{const response=await fetch("/a5-control/stop",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-A5-Control":"stop-v1"},body:JSON.stringify({node:form.dataset.node,target:form.dataset.target,confirmed:true})});
const data=await response.json();result.textContent=data.message||(response.ok?"容器已停止":"操作未完成，请返回面板刷新后核实");if(!response.ok)result.className="error";else button.textContent="已停止";
}catch(error){result.className="error";result.textContent="连接中断，结果暂不确定。请返回面板核实，勿重复提交。";}
});}'''
