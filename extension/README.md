Muse2API Cookie 导入扩展
========================

把当前浏览器里 muse.ai 的登录 Cookie 同步到 muse2api。核心 Cookie 带 httpOnly，只有扩展的 cookies 接口能读到。


Chrome / Edge
-------------
1. 打开 chrome://extensions ，开启「开发者模式」。
2. 「加载已解压的扩展程序」，选中本目录（里面要能直接看到 manifest.json）。
3. 在这个浏览器登录 https://muse.ai/ ，直到能看到聊天界面。
4. 点扩展图标，填入服务地址和 API Key，点「读取并导入」。


Roxy 浏览器
-----------
Roxy 的窗口里不能用「加载已解压的扩展程序」。请用扩展中心：

1. 从管理页下载 muse2api-extension.zip。这个压缩包的根目录就是 manifest.json，不要再套一层文件夹。
2. 打开 Roxy「扩展中心」→「本地上传」，直接选择这个 zip。
3. 上传后把扩展关联到要登录 muse.ai 的项目，再启动该项目窗口。
4. 在这个窗口登录 https://muse.ai/ ，点工具栏上的扩展图标，再点「刷新会话」。

也可以上传本目录这个文件夹。不要上传上一级 muse2api 目录。


常见问题
--------
没读到 Cookie：这个窗口还没登录 muse.ai。
缺 hatch_vml：聊天页开着也可能没有这条。点「刷新会话」，扩展会在这个窗口的 muse.ai 页面请求会话接口，站点重新下发后再读取。
API Key 不对：到管理页重新复制，Key 以 m2a_ 开头。
「刷新会话」只请求当前窗口里的 https://muse.ai/api/session，用来补发 Cookie。复制不会把 Cookie 发到别处。
