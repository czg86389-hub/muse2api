/* 指纹浏览器（Roxy 等）会丢掉只有弹窗、没有后台的扩展。这个文件只负责把扩展登记为已安装。 */
chrome.runtime.onInstalled.addListener(() => {});
