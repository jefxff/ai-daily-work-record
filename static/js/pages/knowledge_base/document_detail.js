/* 服务端数据桥（变量名带文件后缀，避免同页多脚本互相覆盖）：模板里的 <script id="pageData-knowledge_base-document_detail" type="application/json"> 提供 urls/vars。
   以前这些值直接内联在 JS 字符串里，外置后统一走 JSON 数据块（CSP enforce 前提）。 */
var D_knowledge_base_document_detail = (function () {
  var el = document.getElementById('pageData-knowledge_base-document_detail');
  return el ? JSON.parse(el.textContent) : { urls: {}, vars: {} };
})();

window.KB_COPY_MARKDOWN = D_knowledge_base_document_detail.vars['v1'];
