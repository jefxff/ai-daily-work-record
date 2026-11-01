/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-copilot-conversation" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_copilot_conversation = (function () {
  var el = document.getElementById('pageData-copilot-conversation');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

// panel.js 在 window load 时初始化，这里提前挂上目标会话 id
    window.COPILOT_CONVERSATION_ID = D_copilot_conversation.vars['v1'];
