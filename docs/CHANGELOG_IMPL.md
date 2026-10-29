# Changelog de implementação

## F8 — PAPER_FORWARD Binance Spot — 2026-10-17

### Implementado

- RFC curta para o subprojeto único PAPER_FORWARD, com hipótese, feed, licenciamento não verificado, temporalidade/retificações, interfaces, aceite e adiamentos.
- Sessão isolada no módulo `quantlab.paper_forward`: relógio UTC alinhado ao endpoint público, feed WebSocket recente `BTCUSDT` Spot, heartbeat, freshness, verificação de sequência e parada fail-closed.
- Apenas `BUY_HOLD` simples: candle `x=true` iniciado depois da ativação cria sinal com delay positivo; o primeiro aggTrade com horário de origem e receipt posteriores simula fill pelo ledger F3.
- Journal JSONL encadeado por SHA-256, estado atômico, manifest e relatório HTML, todos identificados como `PAPER_FORWARD`; nenhuma API privada, chave ou ordem real.
- Página Streamlit para iniciar/acompanhar/parar uma sessão temporizada e baixar artefatos; CLI `quantlab paper-forward run` para controle manual/terminal.
- Após revisão F7, risco de drawdown reavaliado no open da execução antes da aprovação de fill.
- Limitação: permissão de retenção/redistribuição dos dados públicos não foi verificada; a sessão não armazena candles históricos nem usa daily archive.

### Arquivos alterados

- `src/quantlab/paper_forward.py`, `src/quantlab/paper_replay.py`, `src/quantlab/cli.py`, `src/quantlab/ui/app.py`, `pyproject.toml`, `uv.lock`
- `tests/test_paper_forward_f8.py`, `tests/test_paper_replay_f7.py`, `scripts/smoke_f8_paper_forward.py`
- `docs/rfc-data/RFC-PAPER-FORWARD.md`, `docs/DECISIONS.md`, `docs/MANUAL_QA.md`, `docs/CHANGELOG_IMPL.md`, `README.md`

### Comandos/verificações

- `uv lock --offline` — lockfile resolvido com 44 pacotes; avisos do ambiente: uv-build 0.13.1 fora da faixa declarada e hardlink indisponível, sem erro de resolução.
- `uv lock --check` — lockfile consistente; 44 pacotes resolvidos.
- `uv run python -m unittest discover -s tests -p 'test_paper_*_f[78].py' -v` — 11 testes focados passaram; cobrem timing, candle fechado, gap/sequence, freshness/heartbeat, limite de drawdown incluindo custos projetados, ledger, restart F7 e risco no open.
- `uv run quantlab paper-forward run --help` — CLI registrada com parâmetros de capital, custos hipotéticos, drawdown, delay, duração e root.
- `uv run python -c "import quantlab.ui.app; print('PAPER_FORWARD_UI_IMPORT_OK')"` — importação da UI passou.
- `uv run python scripts/smoke_f8_paper_forward.py --duration-seconds 150` — `F8_PAPER_FORWARD_SMOKE_OK`, sessão `PF-e1c249e1165a`, feed público recente, 2 candles fechados, 1 fill paper posterior, ledger não negativo e journal SHA-256 validado.
- Uma execução anterior do smoke parou fail-closed por `WinError 32` transitório na substituição de `state.json`; temporários passaram a ser exclusivos por gravação com tentativas curtas, e o smoke final passou.

### Status

- `DONE` para o subprojeto F8 PAPER_FORWARD BTCUSDT Spot, com o escopo e as limitações desta RFC; isso não marca os demais candidatos F8 como implementados.
- Limitações: retenção/redistribuição do feed tem licença `NÃO VERIFICADA`; a UI foi importada e validada no código, mas sua interação visual não foi inspecionada manualmente; apenas BUY_HOLD, BTCUSDT Spot e sessão sem reconexão foram aceitos.
- Revisão read-only independente: nenhum bloqueante F8; o auditor observou que o resumo persistido de drawdown F7 continua baseado no último close quando o halt ocorre no open. Os detalhes do evento agora registram marca/equity/drawdown/limite da abertura; a métrica/resumo F7 não foi recalculada fora do escopo.

## F0 — fundação e demo offline — 2026-10-16

### Implementado

- Pacote Python 3.12 instalável por `uv`, com CLI `quantlab --help` e `quantlab demo`.
- Domínio mínimo para Candle, Signal/TargetExposure, SimulatedFill, PortfolioState e RunArtifacts; ledger spot long-only em USDT/base_qty.
- Fixture visual determinística SINTÉTICA com 120 candles e estratégias CASH e BUY_HOLD.
- Artefatos locais por run: JSON, CSV de fills/equity, métricas e relatório HTML autocontido.
- Fixture contábil fixa de compra e venda, separada do sizing de BUY_HOLD.

### Verificações executadas

- `uv sync` — concluído com CPython 3.12.11; o ambiente e o lockfile foram criados. O uv 0.13.1 exibiu um aviso de compatibilidade informativa com a faixa declarada do backend `uv_build`.
- `uv run quantlab --help` — concluído; a ajuda lista o comando `demo`.
- `uv run python -m unittest discover -s tests -p 'test_ledger.py'` — 1 teste focal aprovado: caixa final 1009.79 USDT, fees 0.21 USDT e posição base zero.
- `uv run quantlab demo` — concluído; RUN_ID `20261017T000544Z-6be89385`, 120 candles sintéticos, arquivos locais gerados.
- Relatório `report.html` aberto pelo usuário no navegador; o conteúdo confirma aviso SINTÉTICA, estratégias, equity, fill e arquivos locais.

### Limitações conhecidas

- Nenhum dado real, download, Streamlit, SMA, otimização, ML, futuro, margem, short, paper forward ou corretora.
- CSV é usado para fills/equity nesta demo no lugar de Parquet.
- `BUY_HOLD` usa sizing ilustrativo com passo base de 1e-8; custos e preços sintéticos não representam execução real.
- A reabertura do HTML com a rede desconectada não foi confirmada pelo usuário; o relatório e o SVG são autocontidos e o código não inclui dependências remotas.

## F1 — aquisição oficial controlada — 2026-10-16

### Implementado

- CLI `quantlab data plan`, `fetch` e `import`, limitada a BTCUSDT Spot 1m e até três meses fechados por chamada; o plano mostra URLs oficiais, quantidade de arquivos e tamanho desconhecido sem consulta remota.
- Fetch streaming para arquivos temporários `.part`, retries finitos, tratamento e registro de 404/429, validação manual de cada redirect e allowlist rígida de host, caminho, símbolo, intervalo e mês.
- Verificação do SHA-256 contra `.CHECKSUM`, limites de download/descompressão/razão de compressão, exatamente uma entrada CSV mensal, CRC, rejeição de ZIP Slip e quarentena de conteúdo rejeitado.
- Raw imutável endereçado pela identidade mensal e SHA-256; manifesto JSON e catálogo SQLite guardam origem, período, hash, bytes, ETag, integridade, status, revisão e `supersedes_id`.
- Importação offline passa pelas mesmas validações. Sem checksum registra `source_integrity=UNVERIFIED`; reimportação verificada é idempotente e imprime `SKIPPED_ALREADY_VERIFIED`. `--refresh` consulta novamente a origem para detectar revisão sem sobrescrever objetos.
- Tutorial manual de janeiro de 2024 no README.

### Verificações executadas

- `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_ingestion_integrity.py` — 1 teste focal aprovado, cobrindo checksum, reimportação idempotente, host/arquivo fora da allowlist, ZIP Slip e estado UNVERIFIED sem checksum.
- Smoke offline com ZIP sintético temporário: `data plan` → `data import --checksum` → `data plan --offline` — importação verificada e listagem local confirmadas.
- `.\.venv\Scripts\python.exe -m quantlab data fetch --symbol BTCUSDT --interval 1m --start 2024-01 --end 2024-01` — download oficial real aprovado; ZIP de 2.169.570 bytes, SHA-256 `40b78258091a468f8756843f207f5913eafc1d6e756c40a78711320d93fa5e75`, ETag `"6071234472eec511eaf4e5629348c73a"`, status VERIFIED, revisão 1.
- Repetição de `fetch` e `data import` do mesmo objeto oficial retornaram `SKIPPED_ALREADY_VERIFIED`; `data plan --offline` mostrou 2.169.570 bytes locais.
- `uv sync` não pôde ser executado porque `uv` não está disponível no PATH desta sessão. A verificação usou o ambiente existente em `.venv` com Python 3.12.11; nenhuma dependência foi adicionada nem o `uv.lock` alterado.

### Limitações

- O checksum fornecido por caminho local no modo offline não tem assinatura digital; ele só prova correspondência entre bytes do ZIP e o hash informado.
- F1 valida bytes/estrutura do arquivo, sem analisar as linhas de mercado nem declarar qualidade econômica; isso fica para F2.

## F2 — normalização, qualidade e snapshots — 2026-10-17

### Implementado

- Parser Binance Spot nativo de 12 colunas com detecção explícita de header, validação dos campos e preservação da primeira linha quando não há header.
- Unidade temporal escolhida pela competência F1: ms até 2024-12, us desde 2025-01. Validação do mês, alinhamento, close_time nativo, UTC, `end_at_utc` e `available_at_utc` com delay positivo e `ASSUMED`.
- QA de OHLC, preços, volumes, trade_count, ordenação, duplicatas idênticas/conflitantes, lacunas e cobertura. Lacunas 1m aparecem como intervalos UTC compactos e todas as janelas 1h/4h incompletas, até as sem nenhum minuto, são enumeradas como bloqueadas. Conflito, checksum indisponível/divergente e schema inválido geram relatório/quarentena; o raw não é movido nem regravado.
- Partições mensais Parquet 1m, 1h e 4h com IDs versionados, hash lógico/físico e catálogo SQLite. Agregados só contêm janelas completas de 60/240 candles 1m PASS; janelas incompletas constam como bloqueadas, sem preenchimento.
- CLI `quantlab data audit` materializa/consulta e exibe os resultados, partições, lacunas, cobertura e caminhos dos relatórios JSON/HTML. `quantlab data snapshot` exige cada `partition_id` explicitamente listado, valida os hashes e grava um snapshot imutável com manifesto e QA.
- Leitor F2 `load_backtest_snapshot(root, snapshot_id)` entrega ao tipo já usado pelo núcleo F3 exclusivamente as partições 1m do snapshot nomeado, após conferir hash do manifesto/QA e a lista exata de fontes no catálogo. Nenhuma alteração foi feita no motor F3 ou na engine F0/F1.

### Verificações executadas

- Fixtures sintéticas temporárias 2024/2025: conversão do mesmo instante via ms/us; janeiro/2024 com header e janeiro/2025 sem header preservam a primeira linha; uma hora com 59/60 minutos não sai como agregado PASS; a hora completa confirma OHLC/volume/trades contra os 60 candles-fonte; snapshot conserva IDs exatos e hash raw após acrescentar o fixture de 2025.
- Verificação focal: `uv run python -m unittest discover -s tests -p 'test_data_quality_f2.py' -v` inicialmente expôs dependência não desejada de `pytz`; após mudar timestamps Parquet para TIMESTAMP explicitamente UTC, dois casos passaram e o caso de snapshot expôs junção errada com `raw_objects.object_id`. Só o caso falho foi repetido: `uv run python -m unittest discover -s tests -p 'test_data_quality_f2.py' -k snapshot -v` — 1 aprovado. A verificação de agregação após adicionar os intervalos de gap e o cotejo OHLC foi: `uv run python -m unittest discover -s tests -p 'test_data_quality_f2.py' -k hourly -v` — 1 aprovado. Nenhuma suíte global foi executada.
- Smoke CLI com o objeto Binance Spot oficial já existente, sem baixar dados: `uv run quantlab data import --zip <ZIP 2024-01> --checksum <CHECKSUM>` retornou `SKIPPED_ALREADY_VERIFIED`; `uv run quantlab data audit --raw-object-id binance-spot-BTCUSDT-1m-2024-01-40b78258091a468f8756843f207f5913eafc1d6e756c40a78711320d93fa5e75` gerou Parquet/relatório e exibiu QA; `uv run quantlab data snapshot --partition-id curated-1m-e4d2f806a8fbf9aade259a1b --partition-id curated-1h-a3f6cf77fb8dfc4e550efc61 --partition-id curated-4h-abb12cf2cf4b035693c4c4a2` criou `snapshot-897ee5cbca733bdc684338fe9916460b` com origem REAL e QA PASS.
- A tabela real calculada pelo CLI foi:

  | Período/timeframe | Observados PASS | Esperados | Gaps/bloqueios | Cobertura |
  |---|---:|---:|---:|---:|
  | 2024-01 / 1m | 44.640 | 44.640 | 0 | 100,00000000% |
  | 2024-01 / 1h | 744 | 744 | 0 | 100,00000000% |
  | 2024-01 / 4h | 186 | 186 | 0 | 100,00000000% |

- Conferência real em DuckDB da primeira hora: 60 minutos; OHLC `42283.58 / 42554.57 / 42261.02 / 42475.23`, volume base `1271.68108` e 47.134 trades coincidiram com a partição 1h. O adaptador `load_backtest_snapshot` carregou explicitamente o snapshot com 44.640 candles 1m de `2024-01-01 00:00 UTC` a `2024-01-31 23:59 UTC`.
- Depois de adicionar a verificação do manifesto/QA contra a lista exata do catálogo, `uv run python -m unittest discover -s tests -p 'test_data_quality_f2.py' -k snapshot -v` passou novamente; repetir `create_snapshot` nos mesmos IDs retornou `SKIPPED_ALREADY_EXISTS` e a leitura real continuou `PASS`, 44.640 candles.
- SHA-256 do ZIP raw após o fluxo: `40b78258091a468f8756843f207f5913eafc1d6e756c40a78711320d93fa5e75`, igual ao objeto F1 e ao manifest do snapshot.

### Limitações

- A cobertura pode ser menor que 100% e ainda manter QA PASS para os candles observados; gaps são explícitos e cada janela agregada atingida é bloqueada. QA PASS não significa cobertura perfeita.
- `available_at` é hipótese, não latência observada. CHECKSUM confirma correspondência de bytes, mas somente o fluxo F1 remoto identifica checksum obtido na Binance; arquivo e checksum offline dependem da proveniência fornecida pelo usuário.
- Parquet OHLCV permite consulta e auditoria, mas não mede fila, spread, profundidade nem execução real.

## F3 — backtest spot causal — CLI e smoke real concluídos — 2026-10-17

### Implementado

- CLI `quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h|4h`; requer snapshot F2 nomeado, QA PASS e hashes válidos. Não busca `latest`. Carrega o manifesto integral e os IDs exatos das partições para `sources.json`.
- Parâmetros de caixa, fee, slippage, delay em ms, SMA e breakout configuráveis. O delay padrão vem do snapshot; override é explícito e fica registrado. Delay deve ser positivo.
- Estratégias CASH, BUY_HOLD, SMA_CROSS e BREAKOUT; comparadores CASH e BUY_HOLD usam mesmo ledger, relógio e custos do candidato. Spot long-only, sem leverage, margem, short, limit/partial fill ou corretora.
- Sinais sobre candle 1h/4h completo e fechado; warm-up antes de emitir sinal e reinício após gaps. Ordem no primeiro open 1m estritamente posterior a `available_at`; preço é apenas o open mais slippage adverso. Fee quote separada, sem spread duplicado; saldo compra respeita fee e passo simulado. Posição final aberta fica marcada no close, sem venda forçada.
- Artefatos atômicos: `run.json`, `sources.json`, `strategy.json`, `execution_policy.json`, `signals.csv`, `fills.csv`, `equity.csv`, `metrics.json`, `audit.json` e `report.html`. Exibem IN_SAMPLE/DEMO, origem/QA, configuração e hash, custos, gaps/exemplos omitidos, janelas incompletas, warm-up, vetos, caixa/base e limitações.
- Ajuste mínimo na ponte F2: loader de snapshot também passa o manifesto validado e lista de todas as partições, para o run ser reproduzível sem seleção implícita.

### Verificações executadas

- `uv run python -m unittest discover -s tests -p 'test_backtest_timing.py' -v` — 1 teste focal aprovado: disponibilidade em 15:00:01, primeiro open elegível e fill em 15:01; acrescentar candles futuros não altera os sinais/fills/equity anteriores.
- `uv run python -m unittest discover -s tests -p 'test_ledger.py' -v` — 1 fixture aprovada: compra/venda fixa 1 unidade a 100/110 USDT, caixa final 1.009,79 USDT, fee total 0,21 USDT e posição zero.
- `uv run quantlab backtest --help` — CLI expõe snapshot explícito, quatro estratégias, timeframes 1h/4h e custos/configuração.
- Smoke real, sem download, sobre snapshot F2 `snapshot-897ee5cbca733bdc684338fe9916460b` (BTCUSDT Spot 1m, janeiro/2024, QA PASS, origem REAL): `uv run quantlab backtest --snapshot snapshot-897ee5cbca733bdc684338fe9916460b --strategy sma_cross --timeframe 1h`. Runs reproduzidos `20261017T142344Z-a52fc804` e `20261017T142349Z-32ed49f2`, 44.640 candles 1m, zero gaps, zero janelas 1h incompletas, warm-up 49 janelas, resultado IN_SAMPLE.
- Comparação real `--fee-bps 0` (run `20261017T142355Z-531da343`) vs padrão `--fee-bps 10`: fee SMA foi 0 vs 20,46444081271537525 USDT; equity final foi 924,19274417041355 vs 904,08253653563157475 USDT. Métricas, sinais, fills e equity dos dois runs padrão foram byte a byte idênticos (`metrics.json` SHA-256 `26caad9802b3a835ce2a390a2c95ffe1937e2ece9263207d6fb5805024e5f095`; `signals.csv` SHA-256 `6adfe50346fbe98e3195432ad3b935c73f08658d47035c05700f61ade3301afc`; `fills.csv` SHA-256 `6209e81fecfe3130e5185b760a92fad0ff3675987e7d1a932b02338ff611752c`; `equity.csv` SHA-256 `36fa3cfaa6370cfd660460a608862417d14f4e18696a0eb88a60876b1f3fecd1`).
- Leitura manual de sinais/fills e reconciliação Decimal de todas as compras/vendas: `cash_after` bateu com notional ± fee, quantidades e saldos nunca ficaram negativos, posição terminou em zero e equity final bateu com `cash + base_qty × mark`. Primeiro sinal observado: 02:00:00 → available 02:00:01 → open/fill 02:01:00 UTC.
- Verificação dos artefatos: 10 arquivos esperados presentes; `sources.json` contém manifesto, hash e os três IDs exatos 1m/1h/4h; JSON válido, HTML parseável e avisos de hipótese/OHLCV presentes.
- SHA-256 do ZIP raw após o smoke permaneceu `40b78258091a468f8756843f207f5913eafc1d6e756c40a78711320d93fa5e75`, igual ao catálogo F1/F2; F3 só leu esse raw/partições e publicou artefatos de run.
- Nenhuma suíte global, download, benchmark ou grid search foi executado.

### Limitações

- OHLCV é aproximação de fill no open 1m; não modela fila, profundidade, spread medido, impacto de mercado ou ordens parciais. A proxy de slippage é hipótese separada e não soma spread.
- `available_at` histórico é hipótese configurada, não latência empiricamente medida. Fees e slippage não são tarifas oficiais da Binance.
- Dados reais permanecem evidência `IN_SAMPLE`; métricas históricas não garantem rentabilidade. A posição remanescente é marcada a mercado e não liquidada automaticamente.

## F4 — painel Streamlit e pacote M1 — 2026-10-17

### Implementado

- Painel local com quatro telas: Dados, Backtests, Operações e Runs anteriores. A UI chama os fluxos existentes F0–F3; não contém cálculo de estratégia, execução ou saldo.
- Dados: status vazio/demo/real, origem, objetos raw, cobertura/gaps/QA, auditoria e snapshot; upload mensal com checksum opcional; plano oficial com URLs e tamanho remoto não consultado. Fetch exige confirmação e botão separados.
- Backtests: seleção explícita de símbolo/snapshot/timeframe/estratégia, parâmetros SMA/Breakout, caixa, fee, slippage e delay assumido; chama `load_backtest_snapshot`, `run_backtest` e `write_backtest_artifacts`.
- Operações: filtros de run/estratégia/período, detalhes de sinal/fill/veto e export CSV. O domínio passou a calcular P&L realizado por saída fechada a partir do fluxo de caixa: compra + fee como base, venda − fee como receita; slippage já incluído no notional executado.
- Runs anteriores: prévia/baixar relatório HTML, JSON, métricas e proveniência; runs F0/F3 são localizados por manifest. Curvas Plotly usam equity.csv, benchmark buy & hold e drawdown visual; os arquivos persistidos mantêm os pontos completos.
- README, decisões, checklist manual e `RELEASE_M1.md` atualizados com tutorial e limitações.

### Arquivos alterados

- `pyproject.toml`, `uv.lock`, `README.md`, `RELEASE_M1.md`.
- `src/quantlab/ui/__init__.py`, `src/quantlab/ui/app.py`, `src/quantlab/ui/storage.py`.
- `src/quantlab/backtest.py`, `src/quantlab/backtest_artifacts.py`, `tests/test_backtest_timing.py`.
- `docs/DECISIONS.md`, `docs/CHANGELOG_IMPL.md`, `docs/MANUAL_QA.md`.

### Verificações executadas

- `uv lock` — resolvidos 39 pacotes; lock atualizado com Streamlit 1.66.0, Plotly 6.9.0 e pandas 2.3.3.
- `uv sync --locked` — instalado com sucesso; exibiu apenas o aviso já conhecido de compatibilidade informativa entre `uv_build` 0.12 e uv 0.13.1 e aviso de hardlink entre sistemas de arquivos.
- `uv run python -m unittest discover -s tests -p 'test_backtest_timing.py' -k closed_trade -v` — 1 caso focal aprovado; saída fechada reproduziu P&L realizado 9,79 USDT, com 0,10% de fee em cada lado.
- Smoke local com `streamlit.testing.v1.AppTest`: abriu Dados, navegou Backtests, Operações e Runs anteriores sem exceção; catálogo carregou 8 runs e abriu `20261017T142355Z-531da343` (REAL), com 697 operações lidas dos artefatos.
- Nenhuma suíte geral, lint, download, backtest adicional ou navegação automatizada via browser foi executada.

Comandos exatos da rodada (o primeiro smoke harness foi ajustado para renderizar a página antes de acessar o rádio; a rodada final abaixo passou):

```powershell
$uv = 'C:\Users\bryan\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe'
& $uv lock
& $uv sync --locked
& $uv run python -m unittest discover -s tests -p 'test_backtest_timing.py' -k closed_trade -v
@'
from pathlib import Path
from streamlit.testing.v1 import AppTest
from quantlab.ui.storage import list_runs, load_run

root = Path.cwd()
runs = list_runs(root)
assert runs, "nenhum run local foi encontrado"
loaded = load_run(root, runs[0]["run_id"])
assert loaded["manifest"]["run_id"] == runs[0]["run_id"]
assert loaded["metrics"]
app = AppTest.from_file(str(root / "src" / "quantlab" / "ui" / "app.py"), default_timeout=30)
app.run()
assert not app.exception, [item.message for item in app.exception]
for page in ("Dados", "Backtests", "Operações", "Runs anteriores"):
    app.sidebar.radio[0].set_value(page)
    app.run()
    assert not app.exception, [item.message for item in app.exception]
print(f"UI_OK pages=Dados,Backtests,Operações,Runs anteriores runs={len(runs)} loaded={runs[0]['run_id']} origin={loaded['data_origin']} operations={len(loaded['operations'])}")
'@ | & $uv run python -
```

### Roteiro manual

1. `uv sync`; `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`.
2. Em Dados, execute a demo sintética e confira banner/`RUN_ID`.
3. Planeje um mês Binance e confirme que nada baixa antes de checkbox + botão; depois importe ou baixe um mês.
4. Audite o raw, revise cobertura/gaps/QA e crie snapshot com partições exatas incluindo 1m.
5. Rode SMA e BUY_HOLD no mesmo snapshot, reveja custos hipotéticos, equity, benchmark e drawdown.
6. Filtre Operações, abra uma compra/venda, confira horários, fees/P&L realizado e baixe CSV.
7. Abra Runs anteriores, baixe HTML/JSON, feche/reabra o app e localize os mesmos runs.

### Limitações

- O smoke verificou a navegação e leitura de runs, mas não percorreu cliques de upload, fetch remoto, QA, criação de snapshot ou execução pelo navegador; `docs/MANUAL_QA.md` contém o aceite manual de 8 etapas.
- Fee, slippage e `available_at` seguem hipóteses; OHLCV não comprova execução, e dados históricos são `IN_SAMPLE`.
- Nenhuma funcionalidade F5/M2, ML, paper replay/forward, futures, margem, short, corretora ou ordem real foi adicionada.

## F5 — backfill limitado, comparação e pesquisa M2 — 2026-10-17

### Implementado

- F1/F2 estendidas a Binance Spot BTCUSDT/ETHUSDT e ZIP 1m mensal/diário, com identidades separadas, limites de 3 meses/31 dias e teto agregado explícito de payload que também contabiliza retries. `plan` mostra número de arquivos, URLs oficiais, tamanho local conhecido/desconhecido e pior caso teórico; fetch requer comando/botão confirmado e para na primeira falha.
- Raw continua imutável; checksum, hash, revisões e `supersedes_id` permanecem no catálogo/manifest. F2 encontra cobertura por período de cada objeto diário ou mensal. Snapshots recusam overlaps selecionados e manifestam partições/revisões alternativas sobrepostas.
- CLI e páginas Streamlit **Comparar** e **Pesquisa**. Comparador aceita 2–4 runs, exibe retorno/custos/risco e sinaliza instrumento, período, snapshot ou política de execução diferentes.
- Contrato de pesquisa por família/versão com config e valores permitidos, hash, limite de tentativas, split, janela final, métrica primária/direção/limiar pré-registrados e eventos SQLite. Runs de trial salvam manifest JSON e report HTML.
- Walk-forward de treino crescente/testes seguintes disjuntos em ordem cronológica, sem shuffle; gaps e janelas incompletas são relatados; warm-up usa candles anteriores e cada janela inicia com caixa flat. Métricas por fold: retorno, drawdown, exposição, turnover, fills/custos; inclui stress 2x/3x de fee/slippage. UI/CLI aceitam registrar horizon/purge informativos sem aplicá-los às regras F3 atuais.
- Resultado `NO_GO`, `INCONCLUSIVO` ou `CANDIDATO_A_PAPER` baseado apenas no critério primário pré-registrado. Abertura de holdout requer janela posterior ao desenvolvimento, mesmo instrumento, cobertura integral contígua, alinhamento ao timeframe e warm-up suficiente. Resultado final inclui buy & hold e stress de custo 2x/3x dentro da única abertura. Um snapshot fica reservado a um único papel (desenvolvimento ou holdout) entre versões; a reserva é conferida sob transação no vínculo e antes do trial. Holdout é lacrado, backtests genéricos são bloqueados; versão precisa ser congelada e o evento de abertura é registrado antes do cálculo. Segunda abertura e novos trials são recusados.
- README, decisões e manual QA atualizados. Nenhum backfill histórico longo foi feito.

### Verificações executadas

- `uv run python -m unittest discover -s tests -p test_research_f5.py -v` — 1 teste focal aprovado com ZIPs sintéticos curtos, cobrindo daily/monthly, overlaps listados, fold treino/teste cronológico, custos 2x/3x, horizon/purge informativos, vínculo do holdout, persistência, congelamento, abertura uma vez e bloqueio subsequente.
- O primeiro passe do teste focal encontrou uma junção SQL inválida no inventário de alternativas; foi corrigida para `ON r.object_id = p.raw_object_id` e somente o teste focal foi repetido.
- `uv run python scripts/smoke_f5_ui.py` — criou três runs sintéticos em pasta temporária; AppTest percorreu as seis páginas, aceitou dois runs com mesmo instrumento/período/snapshot/política e mostrou aviso explícito ao comparar snapshot/período diferentes. No mesmo smoke, `data plan` mostrou um ZIP diário ETHUSDT, URLs e teto de 2 MiB sem fetch; não tocou o catálogo do projeto.
- Um primeiro passe do harness de UI falhou ao limpar `TemporaryDirectory` porque o processo ainda estava com o CWD dentro da pasta temporária no Windows. O script agora restaura o CWD em `finally` antes da limpeza; o smoke corrigido passou e remove a pasta temporária.

### Limitações

- Não há tuning automático; usuário seleciona valores F3 dentre os permitidos. Walk-forward mede regras fixas, não treina parâmetros dentro do fold.
- Sem DSR, PBO, White, ML, labels/horizon implementados, simulator de paper ou execução real. Horizon/purge só são campos informativos.
- O catálogo local tem cobertura limitada e não prova um holdout 2026; nenhum resultado final real foi aberto. Backfill e avaliação real aguardam a escolha explícita de símbolo/período/dados pelo usuário.

## F6 — experimento supervisionado causal — 2026-10-17

### Implementado

- Tarefa única de classificação binária: retorno simples do close atual ao close de H barras futuras acima de uma barreira aproximada de fee + slippage round-trip. `label_start`, `label_end`, `label_observable`, `feature_at`, `available_at` e `pred_at` são carimbados em JSON/CSV.
- Builder causal para retorno defasado 1/3/6, volatilidade/range trailing 12, distância SMA20 e volume relativo trailing 20. Janelas usam somente barras fechadas à direita, reiniciam após gaps e não fazem `center=True`, normalização global ou `ffill`.
- Um fold cronológico configurável. Labels de treino só entram se observáveis estritamente antes da validação; amostras purgadas são contabilizadas. Pipeline mediana → StandardScaler → LogisticRegression ajusta somente no treino.
- Controle nulo de prevalência, regressão logística somente preço, logística preço + volume (ablação), thresholds calibrados apenas na primeira subjanela da validação e oito candidatos rastreados, sem otimização de hiperparâmetros. Brier/log loss e curvas de calibração usam a subjanela posterior.
- TargetExposure binário e previsões temporais passam por `run_backtest_window` e o mesmo `_simulate_strategy` F3 que aplica custos, primeiro open elegível e ledger. SMA, cash e buy & hold são reportados no mesmo universo, janela e política. Holdout F5 selado é bloqueado.
- Página Streamlit **ML**, CLI `quantlab ml run`, relatório HTML autocontido, manifest do fold, JSON dos trials e CSV com previsões/labels/features. Se nenhuma variante linear superar nulo e SMA na janela econômica, aparece `SEM GANHO INCREMENTAL`.
- Dependência scikit-learn travada em `pyproject.toml`/`uv.lock`. README, decisões e roteiro manual F6 atualizados.

### Arquivos alterados

- `src/quantlab/ml.py`, `src/quantlab/backtest.py`, `src/quantlab/backtest_artifacts.py`, `src/quantlab/domain.py`, `src/quantlab/cli.py`, `src/quantlab/ui/app.py`
- `pyproject.toml`, `uv.lock`, `README.md`, `docs/DECISIONS.md`, `docs/MANUAL_QA.md`, `docs/CHANGELOG_IMPL.md`
- `tests/test_ml_f6.py`, `scripts/smoke_f6_ml.py`

### Comandos e verificações

- `uv add 'scikit-learn>=1.7,<2'` — dependência e lock resolvidos; scikit-learn 1.9.1 instalado no ambiente local Python 3.12.
- `uv lock --check` — resolveu 44 pacotes; lockfile consistente.
- `uv run python -m unittest discover -s tests -p test_ml_f6.py -v` — `Ran 1 test ... OK`. Altera valores sintéticos no fim do dataset e confirma probabilidades invariantes para previsões anteriores.
- `uv run python scripts/smoke_f6_ml.py` — `F6_SMOKE_OK`, treino 74, validação 94, 8 trials; pipeline sintético treino → previsão → backtest F3 → JSON/CSV/HTML concluído; AppTest abriu a página ML e leu o resultado salvo. O exemplo foi marcado `DEMO`; o status “GANHO INCREMENTAL OBSERVADO NA VALIDAÇÃO” é apenas saída da fixture sintética e não resultado financeiro real.
- `uv run quantlab ml run --help` — mostrou as opções de snapshot, datas, timeframe, H e custos.
- O primeiro teste focal encontrou subtração Decimal/float na volatilidade; corrigida e somente o teste focal foi repetido.
- Os primeiros passes do smoke encontraram campos ausentes no manifest usado pelo relatório; `threshold_trials` e `trial_count` foram adicionados e somente o smoke foi repetido até concluir. O aviso de largura de dataframe da versão atual do Streamlit foi resolvido usando `width="stretch"` na página ML.

### Roteiro manual

1. Rode `uv sync` e `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`.
2. Em **ML**, selecione snapshot QA PASS não selado, revise H, custos e intervalos UTC de treino/validação e execute uma vez para a hipótese escolhida.
3. Inspecione `artifacts/ml/<experiment_id>/experiment.json`: confira fold, purge, maior `label_observable_at` de treino e `fit_only_train=true`.
4. Abra `predictions.csv` e confira `pred_at`, `feature_at`, `available_at`, `label_start`, `label_end`, label observado e features.
5. Compare nulo, logística preço, logística preço + volume e SMA; baixe o HTML e CSV. Verifique `SEM GANHO INCREMENTAL` quando aplicável.
6. Para a alteração futura controlada, rode o teste focal; para o fluxo completo sintético, rode o smoke F6.

### Limitações e evidência

- Implementado e verificado com fixture sintética para DEMO e estabilidade de previsões anteriores. Nenhum conjunto real foi selecionado ou baixado nesta fase; portanto não há resultado de desempenho em mercado real.
- Um fold, um snapshot e um instrumento; seleção entre duas variantes e calibração de thresholds dentro da validação limitam independência. Threshold é escolhido na validação inicial e sua métrica financeira é medida na parte posterior da mesma validação, não em holdout.
- A barreira do label é aproximada; fee/slippage e disponibilidade são premissas, OHLCV/open de 1m não prova preço de execução. Métricas Brier/log loss/calibração e acurácia não estabelecem lucro futuro.
- Sem deep learning, RL, dados novos, holdout, corretora, ordens ou paper trading. A implementação F6 não antecipa fases futuras.

## F7 — Paper Replay histórico offline — 2026-10-17

### Implementado

- Controller sequencial de candles 1m para regras `CASH`, `BUY_HOLD`, `SMA_CROSS` e `BREAKOUT`, com configuração congelada por hash. SMA/breakout recebem uma `StrategyView` causal com decision candles completos até o relógio atual; nunca consultam barras futuras.
- Fills reutilizam `execute_market_target` F3, inclusive open 1m estritamente posterior a `available_at`, sizing, fee quote, slippage proxy e step de quantidade. `PAPER_REPLAY` fica visível em UI e exportações.
- Estados `STOPPED`, `READY`, `RUNNING`, `DEGRADED` e `HALTED`, razões e transições persistidos. Gap, barra elegível ausente, drawdown máximo, saldo/ledger inválido, snapshot selado como holdout, duplicidade de event ID e divergência bloqueiam nova execução conforme o gate.
- Journal encadeado por SHA-256, IDs estáveis para sinais/fills, restrições de unicidade, checkpoint transacional por lote, equity por candle e reconciliação por reexecução do ledger ao retomar/reabrir. Reset confirmado cria sessão nova e preserva o journal original.
- Página Streamlit **Paper Replay** com início, pausa, retomada, parada, reset revisado, relógio UTC, estado/razão, posição/cash/equity/drawdown, log, comparação com F3 no mesmo snapshot/prefixo e exportações de journal JSON, equity CSV e relatório HTML autocontido.
- `RiskGate` padrão: exposição máxima 100% e drawdown 30%, ambos congelados e editáveis na sessão. Não há liquidação automática ao atingir o limite. Holdouts selados ficam bloqueados.
- `README.md`, decisões, manual QA, teste focal de F7 e smoke sintético atualizados. Sem endpoint, socket, chave privada ou ordem real.

### Arquivos alterados

- `src/quantlab/paper_replay.py`, `src/quantlab/ui/app.py`
- `tests/test_paper_replay_f7.py`, `scripts/smoke_f7_replay.py`
- `README.md`, `docs/DECISIONS.md`, `docs/MANUAL_QA.md`, `docs/CHANGELOG_IMPL.md`

### Comandos e verificações

- `uv run python -m unittest discover -s tests -p test_paper_replay_f7.py -v` — 4 testes passaram: duplicidade/restart sem fill ou fee duplicada, gap que bloqueia candle posterior, veto de exposição e invariância de sinal quando candles futuros são alterados.
- `uv run python scripts/smoke_f7_replay.py` — `F7_SMOKE_OK`; AppTest iniciou, pausou e retomou a UI sem segundo fill, comparou igual ao F3 no mesmo snapshot sintético, exportou journal JSON/equity CSV/HTML e abriu outra sessão sintética em que a página persistiu `HALTED` por gap. Origem `SYNTHETIC`, evidência `PAPER_REPLAY`.
- O AppTest imprime avisos `missing ScriptRunContext` em bare mode, mas terminou com sucesso. Um passe anterior revelou que o fixture mantinha aberta a conexão SQLite de preparação; a conexão agora é fechada explicitamente e a execução final terminou sem erro de limpeza.

### Roteiro manual

1. Rode `uv sync` e `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`.
2. Em **Paper Replay**, selecione snapshot QA PASS fora de holdout, revise estratégia, fee, slippage, delay e limites, e clique **Congelar e iniciar replay**.
3. Confira relógio UTC/ledger. Pause, reinicie o app, escolha a sessão e retome após a reconciliação automática de journal, fills, fees e equity.
4. Use snapshot QA PASS com gap sintético para confirmar `HALTED` e nenhum fill após a lacuna. Leia a razão, confirme a revisão e use **Reset explícito** para criar outro ID.
5. Pause/encerre, compare com F3 no mesmo snapshot e intervalo, baixe journal JSON, equity CSV e relatório HTML; confira `cash + base_qty × mark = equity` e fees contra fills.

### Limitações e evidência

- Verificado somente com candles sintéticos; nenhum snapshot real foi selecionado, nenhum dado foi baixado e nenhum resultado financeiro real foi produzido.
- F7 não executa inferência ML F6 dentro do relógio; somente regras F3 determinísticas são aceitas. Resultado é histórico offline `PAPER_REPLAY`.
- Dados `available_at`, taxas e slippage continuam hipóteses. OHLCV não prova fill. O limite de drawdown interrompe o replay e conserva a posição simulada sem encerramento automático.
- O smoke usa AppTest e APIs do controller; não substitui inspeção manual visual no navegador nem valida a reinicialização de um processo Streamlit real. O roteiro manual cobre esse aceite.

## F1/F3/F5 — estratégias comparáveis e pesquisa inicial — 2026-10-29

### Implementado

- F1 valida dias já fechados do mês corrente pela identidade diária correta; foi corrigida a rejeição indevida pela regra de mês mensal. A allowlist de host/path/símbolo e a verificação de CHECKSUM, SHA-256 e CRC permanecem ativas.
- F3 amplia as regras disponíveis para SMA crossover, breakout, momentum e mean reversion em 15m, 1h, 4h e 1d, reaproveitando as velas 1m, o ledger, o fill causal, fees, sizing e snapshots existentes.
- F3 registra Sharpe diário, convenção/amostras, win rate, profit factor e trades fechados em JSON/HTML, expondo os parâmetros e métricas também na CLI e na página Backtests.
- F5 suporta as quatro famílias/timeframes e os parâmetros variáveis pré-registrados, com colunas adicionais de Sharpe/win rate/profit factor nos folds. Walk-forward continua cronológico e registra estresse de custos 2x/3x.
- Foram coletados mais 6 ZIPs mensais (BTC/ETH julho a setembro) e 56 ZIPs diários (BTC/ETH 1–28 de outubro). O relatório comparativo registra os 218 objetos F1 verificados, 17 resultados F3 bem-sucedidos, seis trials F5, resultados negativos, estados e limitações sem pontuar o holdout final.
- Foi corrigida a impressão Windows do fator sqrt(365), trocando o símbolo U+221A por texto ASCII após um run ter persistido métricas e falhado somente ao imprimir no console. A reexecução concluiu normalmente.

### Arquivos alterados

- src/quantlab/domain.py, src/quantlab/backtest.py, src/quantlab/backtest_artifacts.py
- src/quantlab/data_ingestion.py, src/quantlab/research.py, src/quantlab/cli.py, src/quantlab/ui/app.py
- tests/test_ingestion_integrity.py
- README.md, .gitignore, docs/DECISIONS.md, docs/MANUAL_QA.md, docs/CHANGELOG_IMPL.md
- docs/RELATORIO_PESQUISA_INICIAL_2026-10-29.md

### Comandos e verificações

- Aquisição limitada e checksums: quatro comandos uv run quantlab data fetch para monthly 2026-07 a 2026-09 e daily 2026-10-01 a 2026-10-28 em BTCUSDT/ETHUSDT; resultados e comandos completos estão no relatório de pesquisa.
- uv run python -m unittest discover -s tests -p test_ingestion_integrity.py -k closed_day_in_the_current_month — 1 teste focal aprovado.
- uv run python -m unittest discover -s tests -p test_backtest_timing.py — 2 testes aprovados.
- uv run python -m unittest discover -s tests -p test_research_f5.py -v — 1 teste aprovado.
- uv run python scripts/smoke_f5_ui.py — F5_SMOKE_OK; páginas, comparação e planejamento diário conferidos.
- uv run quantlab backtest — 17 configurações F3 completas com saída e artefatos válidos; comandos, run IDs e métricas estão em docs/RELATORIO_PESQUISA_INICIAL_2026-10-29.md.
- uv run quantlab research trial — seis trials F5 completos; métricas por fold e stress 2x/3x em artifacts/research/<family>-vN/<trial-id>/trial.json.

### Roteiro manual

1. Execute uv sync e uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1.
2. Em Dados, planeje/baixe arquivos fechados, audite cada objeto F2 e crie snapshot apenas com partições 1m QA PASS exatas.
3. Em Backtests, selecione ativo, snapshot, família, timeframe, parâmetros e custos; rode, revise HTML/JSON e compare contra buy-and-hold.
4. Em Pesquisa, pré-registre família/versão, cortes, parâmetros permitidos, critério e holdout antes de iniciar os trials. Congele somente conforme os critérios registrados; o holdout final abre uma vez.

### Limitações e evidência

- F2 existente segue gapless apenas no trecho utilizado de 2023-04 até 2026-06; julho a outubro de 2026 são raw F1 sem QA/snapshot. O teste 2025-04 a 2026-04 foi ligado a uma versão F5 que terminou NO_GO, permaneceu selado e não foi pontuado. Nenhuma versão foi congelada; o holdout final não foi aberto.
- F3 full-year é IN_SAMPLE para os períodos de treino/validação. O walk-forward de um ano foi montado depois de visualizar métricas anuais, e o walk-forward de 24 meses inclui uma validação já vista; ambos são diagnósticos exploratórios.
- Fee 10 bps, slippage 5 bps por lado e atraso 1s são hipóteses. Não há spread separado, evidência de fills reais, classificação causal de regime, bootstrap, DSR, PBO, Reality Check ou teste de capacidade.
- Backtests históricos não provam lucratividade futura. Não foram implementadas corretora, ordens reais ou chamadas de compra/venda.

## Otimização focal de pesquisa F5 — 2026-10-29

### Implementado

- `run_backtest_window` agora permite selecionar os baselines necessários. F5 deixa de simular CASH nas janelas de treino/teste e deixa de simular CASH e BUY_HOLD nos stresses 2x/3x; os resultados usados pelos relatórios continuam presentes. Chamadas existentes mantêm o padrão completo por default.
- Adicionado `prepare_backtest` para agregar candles e calcular os sinais causais uma vez por trial. F5 passa essa preparação ao backtest principal, à geração de folds e às janelas walk-forward. Cada simulação recebe novos eventos de sinal, preservando o isolamento do ledger e do estado mutável do evento.
- Janelas de avaliação agora usam busca binária nos candles ordenados do snapshot em vez de varrer toda a série para localizar início/fim.
- O fluxo de ledger continua serial por carteira. DuckDB já usa quatro threads neste computador para consultas; não foi criado paralelismo de processo, dependência GPU ou engine alternativo.

### Arquivos alterados

- `src/quantlab/backtest.py`
- `src/quantlab/research.py`
- `tests/test_backtest_timing.py`
- `docs/DECISIONS.md`
- `docs/CHANGELOG_IMPL.md`

### Comandos e verificações

- `uv run python -m unittest discover -s tests -p test_backtest_timing.py -v` — 3 testes passaram, incluindo comparação de métricas/fills/equity entre execução completa, baseline seletivo e sinais preparados.
- `uv run python -m unittest discover -s tests -p test_research_f5.py -v` — 1 teste F5 passou; inclui a execução de um trial sintético, folds e o bloqueio do holdout para abertura repetida.
- Medição de desempenho foi executada com um script Python temporário via `uv run python -`, sobre `snapshot-6128f04036bd63e5480e52ca49e0db99` (527.040 candles de desenvolvimento BTC), uma janela walk-forward de 60 dias em 4h e custos de 1x/2x/3x. Não foram escritos runs/trials, alterado SQLite, aberto holdout, feito download ou chamado endpoint de ordem.
- Antes: três janelas com todas as simulações de baseline — 7,76 s. Depois: preparo + folds 1,13 s e as três janelas com apenas as simulações necessárias 1,69 s — 2,82 s no total, redução medida de 63,7%. Retorno SMA verificado idêntico nos dois lados: -5,5292163593%, -7,2144800271% e -8,8696858879% respectivamente. O teste apenas confirmou equivalência de código; estes valores continuam sendo resultados históricos exploratórios.
- Carregar/verificar/materializar o snapshot mediu 11,63–15,18 s em execuções distintas e não foi otimizado nesta alteração. DuckDB informou `current_setting('threads') = 4`; o host tem Intel i3-9100F, 4 núcleos/4 threads e GPU GTX 1660 6 GB. O backtester usa `Decimal` e estado dependente do tempo, sem operação adequada para GPU. Threads não aceleram esses loops Python sob o GIL; processos no Windows precisariam serializar/copiar o snapshot. Não foi alegado uso paralelo do backtester nem aceleração por GPU.

### Roteiro manual

1. Abra a aplicação com `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`.
2. Em **Pesquisa**, selecione uma versão OPEN e seu snapshot de desenvolvimento; mantenha o holdout SELADO.
3. Execute um trial pré-registrado. O backtest completo mantém CASH/BUY_HOLD; os folds mantêm BUY_HOLD para comparação e calculam os stresses somente para a estratégia avaliada.
4. Confira o `trial.json` e `report.html`, compare número de folds, parâmetros, custos e resultados com o trial registrado. A otimização não muda limites, sinais, taxas, fills ou critério de seleção.

### Limitações

- O benchmark mediu uma janela real já usada para desenvolvimento, não toda a execução `record_trial`; o tempo total de um trial, RAM de pico e desempenho em outros ativos/timeframes não foram medidos.
- Não houve benchmark GPU ou multiprocessing. Com este padrão de ledger sequencial, isso não se justifica sem uma operação vetorizável medida e um desenho que não replique memória sem controle.
- Nenhum resultado do holdout final foi aberto. Backtests permanecem históricos exploratórios e não demonstram rentabilidade futura.
