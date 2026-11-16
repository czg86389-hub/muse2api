Muse2API Cookie 导入扩展 —— 安装说明
=====================================

这个扩展只做一件事：把你浏览器里 muse.ai 的登录 Cookie
同步到你的 muse2api 服务。不用装 Python、不用开终端。


一、解压
--------
把 muse2api-extension.zip 解压到一个你不会删掉的目录，例如：

    Windows : D:\muse2api-extension
    macOS   : ~/Documents/muse2api-extension

解压后应该能看到 manifest.json、popup.html、popup.js 三个文件。


二、在 Chrome / Edge 里加载
---------------------------
Chrome：
  1. 地址栏输入  chrome://extensions  回车
  2. 打开右上角的「开发者模式」开关
  3. 点左上角「加载已解压的扩展程序」
  4. 选中第 1 步解压出来的那个文件夹（不是里面的单个文件）
  5. 工具栏出现一个拼图图标，把它固定到工具栏（可选但方便）

Edge：
  1. 地址栏输入  edge://extensions  回车
  2. 打开左下角「开发人员模式」
  3. 点「加载解压缩的扩展」
  4. 其余同上

其他 Chromium 内核浏览器（Brave / Vivaldi / 360 极速 等）步骤类似。


三、使用
--------
1. 先在这个浏览器里打开 https://muse.ai/ 并登录，
   登录到能看到聊天界面为止。
2. 到 muse2api 管理页「账号池」页顶部，复制 BASE URL 和 API Key。
3. 点浏览器工具栏上的扩展图标，把这两项填进去（只需要填一次，会记住）。
4. 在登录 Muse 的同一个窗口中点「读取并导入」。无痕/InPrivate 使用前，需要在扩展详情中开启「允许在无痕模式下运行」。
5. 检查结果中的「Cookie 来源」是否为预期窗口，再到管理页测试账号。导入成功只表示保存成功，不代表会话已验证。


四、常见问题
------------
Q: 无痕窗口已登录，但导入了普通窗口账号，或测试提示 /api/session HTTP 401
A: 1.0.2 及更早版本没有按当前窗口选择 Cookie 存储区，可能读到普通窗口里的旧会话。
   从你的 Muse2API 管理页下载 1.0.3，替换原扩展目录并重新加载；在登录 Muse 的同一窗口重试。
   1.0.3 找不到对应存储区时会停止导入，不会回退到其它窗口。已注销的会话仍需要在官网重新登录。

Q: 提示「没读到 muse.ai 的 Cookie」
A: 说明这个浏览器里还没登录 muse.ai。先打开 https://muse.ai/ 登录。

Q: 提示「缺核心项 hatch_sess 等」
A: 同上 —— 登录没完成。确认能看到聊天界面再点。

Q: 提示「API Key 不对（服务返回 401）」
A: 到管理页重新复制一次 API Key。注意 Key 以 m2a_ 开头。

Q: 提示跨域 / 网络错误
A: 检查服务地址是不是 https，以及服务本身能不能打开
   （浏览器直接访问 服务地址/admin 试试）。

Q: 会不会把我的 Cookie 传到别的地方？
A: 不会。扩展使用 cookies、storage 和 activeTab 权限，代码就在 popup.js 里，
   你可以自己看：它只往你填的那个服务地址发一个 POST，没有别的请求。


五、卸载
--------
chrome://extensions → 找到「Muse2API Cookie 导入」→ 移除。
