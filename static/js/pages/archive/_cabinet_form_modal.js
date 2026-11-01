(function () {
        var layersInput = document.getElementById('id_layers');
        var row = document.getElementById('layerCellsRow');
        var initial = [];
        try { initial = JSON.parse(document.getElementById('initialLayerCells').value || '[]'); } catch (e) { initial = []; }

        function esc(s) {
            return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
        }

        function renderCells() {
            if (!layersInput || !row) return;
            var n = parseInt(layersInput.value, 10);
            if (!n || n < 1 || n > 12) { row.innerHTML = '<span class="arc-hint">请先填写 1-12 的层数</span>'; return; }
            var html = '';
            for (var i = 1; i <= n; i++) {
                var v = initial[i - 1] !== undefined ? initial[i - 1] : '';
                html += '<div style="text-align:center">'
                    + '<div style="font-size:0.72rem;color:var(--c-text-secondary);margin-bottom:2px;">第 ' + i + ' 层</div>'
                    + '<input class="form-control" aria-label="每层格数" style="width:74px;padding:0.35rem 0.5rem;font-size:0.85rem;" type="number" min="1" max="99" '
                    + 'name="layer_cell_' + i + '" value="' + esc(v) + '" placeholder="10">'
                    + '</div>';
            }
            row.innerHTML = html;
        }
        if (layersInput) {
            layersInput.addEventListener('input', renderCells);
            renderCells();
        }
    })();
