# Roteiro manual — F0

1. Na raiz do projeto, execute `uv sync`; espere a criação do ambiente local sem dependências de runtime externas.
2. Execute `uv run quantlab --help` e confirme que o subcomando `demo` aparece.
3. Execute `uv run quantlab demo`; confirme que a saída marca a origem como `SINTÉTICA` e imprime um `RUN_ID`.
4. Abra `artifacts/<RUN_ID>/report.html` diretamente no navegador. Confirme o selo **DADOS SINTÉTICOS**, as seções Resumo/Equity/Fills/Arquivos e as linhas CASH e BUY_HOLD.
5. Abra `fills.csv`, `equity.csv`, `metrics.json` e `run.json` no mesmo diretório. Confira `data_origin=SYNTHETIC`, fee e proxy de slippage separados e marcação de posição no final.
6. Feche e reabra `report.html` com a rede desligada; a página e o SVG devem continuar disponíveis localmente.
7. Execute `uv run python -m unittest discover -s tests -p 'test_ledger.py'`; o caso de quantidade fixa deve terminar com 1009,79 USDT, fee total de 0,21 USDT e base_qty zero.

O P&L mostrado na demo serve para exercitar o software sobre dados inventados. Não é evidência de vantagem ou recomendação financeira.

## Roteiro manual — F2

1. Execute `uv sync` e confirme que `uv run python -c "import duckdb; print(duckdb.__version__)"` mostra a versão fixada no `uv.lock`.
2. Para o raw F1 já baixado de `2024-01`, execute `uv run quantlab data audit --symbol BTCUSDT`. Confirme `timestamp_unit=ms`, primeira vela em 2024-01-01 UTC, última vela dentro do mês, `source_close_time_raw` presente, delay positivo ASSUMED e cobertura/gaps calculados.
3. Copie os `partition_id` QA PASS impressos e confirme em `artifacts/data-quality/<RAW_OBJECT_ID>/` o relatório JSON e HTML. Os gaps devem aparecer com observados/esperados; não são completados.
4. Abra o Parquet 1h com DuckDB e confira uma janela completa: a barra 1h tem open do primeiro minuto, high máximo, low mínimo, close do último minuto e volumes/trades somados. Para 4h, confirme 240 candles fonte.
5. Remova um minuto somente de uma fixture em diretório temporário, rode import→audit e confirme que a barra agregada daquela hora não consta no Parquet PASS e que a janela aparece `INCOMPLETE_BLOCKED`. Nunca edite o ZIP em `data/raw/`.
6. Execute `uv run quantlab data snapshot --partition-id <ID_1M>` (e repita `--partition-id` para outras partições desejadas). Confirme `snapshot.json` e `qa.json` guardam os mesmos IDs exatos; não há escolha implícita de `latest`.
7. Compare o SHA-256 do ZIP em `data/raw/` antes/depois e com `raw_sha256` no catálogo/manifest; todos devem ser iguais. A consulta ao snapshot deve receber o `SNAPSHOT_ID` explicitamente.

`available_at` é uma hipótese de disponibilidade histórica e CHECKSUM não é assinatura digital do arquivo. QA PASS atesta os registros observados, não cobertura perfeita nem executabilidade econômica.

## Roteiro manual — F3

1. Copie um `SNAPSHOT_ID` QA PASS da saída de `uv run quantlab data snapshot`. Não existe seleção implícita de `latest`.
2. Execute `uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h`. Confirme `RUN_ID`, origem, QA, `IN_SAMPLE`, atraso `ASSUMED`, política de fill, gaps, warm-up, vetos, fees, slippage e comparadores CASH/BUY_HOLD.
3. Repita com `--strategy momentum --timeframe 1d --momentum-lookback 24`, `--strategy breakout --timeframe 15m --breakout-lookback 20` e `--strategy mean_reversion --timeframe 1h --mean-reversion-lookback 30 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0`. Cada run deve salvar o timeframe/parâmetros exatos e as métricas Sharpe diário, win rate, profit factor e trades fechados; valores indefinidos devem aparecer como `N/D`/`null`.
4. Abra `artifacts/backtests/<RUN_ID>/report.html`; o relatório alerta que disponibilidade histórica e custos são hipóteses, que OHLCV aproxima fills e que o resultado não garante rentabilidade.
5. Compare `--fee-bps 0` com `--fee-bps 10`; confira que as fees saem de zero para positivo e que equity/P&L líquido refletem o custo.
6. Rode novamente o comando com os mesmos parâmetros. Compare os blocos de métricas e o `fills.csv` dos dois runs; `RUN_ID` e horário de criação são metadados novos.
7. Inspecione 3 sinais em `signals.csv`: cada `eligible_at_utc` deve ser o primeiro open 1m estritamente posterior a `available_at_utc`. Para uma decisão às 15:00:00 UTC com delay de 1.000 ms, o primeiro fill elegível é às 15:01:00, nunca às 15:00:00.
8. Para uma compra/venda, confira `cash_after = cash_before − quote_amount − fee` na compra e `cash_after = cash_before + quote_amount − fee` na venda. Confirme `equity = cash + base_qty × mark` em `equity.csv`; posição aberta ao fim fica marcada, não liquidada.

O backtester só faz spot long-only integral a mercado simulado; não modela fila, spread, profundidade, ordens parciais, margem nem alavancagem. A evidência com histórico é `IN_SAMPLE`.

## Roteiro manual — F4 / M1

1. Na raiz do projeto, execute `uv sync` e `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`. Confirme que **Dados** abre, mostra o estado local vazio/demo/real e explica a próxima ação, sem traceback na tela.
2. Clique **Executar demo sintética**. Confirme o banner `DADOS SINTÉTICOS`, copie o `RUN_ID` e abra **Runs anteriores**; o relatório HTML, `run.json` e `metrics.json` devem estar disponíveis para prévia/baixar.
3. Em **Dados**, informe `2024-01` em **Preparar plano**. Confirme nome do ZIP, URLs oficiais, 1 ZIP + 1 CHECKSUM e tamanho remoto não consultado. Verifique que não houve download antes do checkbox e do botão de confirmação.
4. Confirme o mês somente quando quiser buscar pela rede, ou envie o ZIP mensal correspondente com o nome oficial e `.CHECKSUM`. Em **Auditar**, confirme estado de checksum, unidade/timestamps, cobertura, gaps, QA e caminho JSON/HTML.
5. Selecione explicitamente as partições QA PASS que incluem 1m e crie um snapshot. Confirme `SNAPSHOT_ID`, origem, IDs exatos, delay `ASSUMED` e QA; não transforme objeto UNVERIFIED em snapshot real.
6. Em **Backtests**, selecione símbolo/snapshot, `1h`, SMA e capital/custos; execute. Confirme `RUN_ID`, equity, drawdown, benchmark BUY_HOLD e aviso de custos hipotéticos. Execute uma segunda comparação com BUY_HOLD no mesmo snapshot e política.
7. Em **Operações**, filtre SMA e BUY_HOLD, abra operações de compra e venda e confira `signal_at < available_at < fill_at`, preço, qty, fees, P&L realizado na saída fechada, veto/reason. Baixe o CSV filtrado e compare alguns registros com `fills.csv`/`signals.csv`.
8. Feche e reabra o app. Em **Runs anteriores**, reencontre os IDs, proveniência do snapshot/política e relatórios HTML/JSON; os artefatos locais devem continuar disponíveis.

Custos e atraso continuam hipóteses; OHLCV é proxy de execução e `IN_SAMPLE` não prova rentabilidade. ML e paper forward permanecem fora deste roteiro.

## Roteiro manual — F5 / pesquisa M2

### Dados e backfill limitado

1. Abra **Dados**; verifique na tabela Raw símbolo, granularidade, dia/mês, revisão, checksum, tamanho e URL.
2. Prepare um intervalo fechado pequeno para BTCUSDT ou ETHUSDT. Confira quantidade de ZIPs, URLs, estado local e teto MiB. Sem a caixa de confirmação e o botão **Baixar intervalo planejado**, nenhum fetch começa.
3. Em desenvolvimento, prefira um dia/um mês. Não solicite 2020–2026. Após aquisição, audite e examine cobertura e gaps.
4. Tente incluir no mesmo snapshot uma partição mensal e outra diária que cubram as mesmas barras: o sistema deve recusar `SNAPSHOT_OVERLAP`. Se fontes alternativas não forem selecionadas, confira a exclusão de overlap no manifest.
5. Crie um snapshot com partições exatas não sobrepostas. Repita com BTC e ETH se houver cobertura; compare os manifests e observe diferenças de início/fim/gaps.

### Comparação de runs

1. Salve dois runs com o mesmo símbolo, snapshot, período e política de execução; abra **Comparar**, selecione ambos e confira retorno, drawdown, exposição, turnover, fees e slippage.
2. Selecione outro run com período/snapshot/instrumento ou custos diferentes. Confirme o aviso explícito e leia os campos de cobertura e execução por linha.

### Walk-forward e holdout

1. Em **Pesquisa**, registre uma nova família/versão usando um snapshot de desenvolvimento e escolha uma métrica primária, direção, limiar, orçamento de tentativas e períodos antes de executar trial. Campos horizon/purge podem ser registrados para modelos futuros; confira que são informativos e não alteram os folds F5.
2. Tente registrar uma janela holdout sobreposta ao fim do desenvolvimento; o contrato deve ser recusado. Uma janela posterior deve ter snapshot separado, mesma moeda/ativo, cobertura contígua completa e warm-up suficiente.
3. Execute um fold curto. Confira no JSON/HTML o config hash, split cronológico, métricas de treino/teste, custo 2x/3x, cobertura e exclusões. O holdout continua `SELADO`.
4. Congele um trial completo. Tente registrar outro trial: a versão deve recusar tuning. O congelamento também precisa recusar candidato cujo warm-up exigido não caiba no snapshot final.
5. Confirme a abertura final uma vez. Confira evento `HOLDOUT_OPENED`, hash do snapshot, config congelada, benchmark buy & hold e custos 2x/3x. Tente abrir de novo e confirme rejeição; o estado mantém edição bloqueada mesmo em falha.

### Verificações focais registradas para F5

- `uv run python -m unittest discover -s tests -p test_research_f5.py -v` — cria arquivos sintéticos mensais/diários curtos, cobre sobreposição, partição cronológica, horizon/purge informativos, custo 2x/3x, vínculo/congelamento do holdout, abertura única e bloqueio de tuning.
- `uv run python scripts/smoke_f5_ui.py` — cria três runs sintéticos em pasta temporária; AppTest percorre as seis páginas, confirma comparação compatível e aviso para período/snapshot diferentes, e chama `data plan` para um ZIP diário ETH com limite de 2 MiB. Não faz fetch nem altera o catálogo deste projeto.
- Para abrir o painel manualmente, use `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1` e encerre com `Ctrl+C`.
- Não houve download grande nem avaliação holdout com dados históricos reais nesta fase. A cobertura do catálogo atual não representa 2026-01–09; resultado final real continua pendente até que o usuário escolha e adquira um período elegível.

### F6 — modelo supervisionado

1. Abra a página **ML** ou execute `uv run quantlab ml run` com um snapshot QA PASS não selado e intervalos UTC contíguos de treino e validação. Use somente períodos já presentes no snapshot.
2. Abra `artifacts/ml/<experiment_id>/experiment.json`. Confirme que `training_labels_observable_before_validation=true`, `fit.fit_only_train=true`, o máximo `fit_label_observable_at` é anterior ao início da validação e os labels que cruzaram a fronteira foram contados como purge.
3. Inspecione uma linha de `predictions.csv`: `prediction_at_utc` deve coincidir com `feature_available_at_utc`; `label_end_at_utc` e `label_observable_at_utc` devem ser posteriores ao `label_start_at_utc`. Labels da validação não participam do ajuste; labels só entram na calibração inicial ou em métricas posteriores.
4. Confira os oito thresholds registrados em `threshold_trials.json`, todos definidos no intervalo inicial de calibração. A janela econômica começa depois da calibração; holdout selado deve ser recusado.
5. No relatório HTML, compare `ML_NULL`, preço-only, preço+volume, `SMA_CROSS` e buy & hold; confirme mesmo snapshot, período, fee, slippage e política F3. Se ambas as variantes lineares não superarem o nulo e SMA, deve aparecer `SEM GANHO INCREMENTAL`.
6. Rode `uv run python -m unittest discover -s tests -p test_ml_f6.py -v`; o teste altera artificialmente os valores do fim da fixture e compara as probabilidades anteriores. Rode `uv run python scripts/smoke_f6_ml.py` para treino → predição → backtest → report/CSV/JSON sintéticos. Faça download de `predictions.csv` e do HTML na página ML.

## Roteiro manual — F7 / Paper Replay

1. Rode `uv sync` e `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`. Em **Paper Replay**, selecione um snapshot QA PASS não selado; escolha BUY & hold ou uma regra congelada, confira fees, slippage, delay, exposição e drawdown. O aviso `PAPER_REPLAY` deve estar visível.
2. Clique **Congelar e iniciar replay**. Confira estado `RUNNING`, relógio simulado UTC, posição/cash/equity e eventos de criação/checkpoint. Compare o snapshot ID e a configuração/hash no journal exportado.
3. Clique **Pausar** e feche/reinicie o app. Selecione a mesma sessão; a abertura deve reexecutar o journal, conferir hashes, fills, fees, saldos e equity e manter o mesmo relógio. Clique **Retomar** e confirme que não surgiu segundo fill nem fee duplicada.
4. Em um snapshot sintético de QA PASS com um minuto removido entre duas barras, inicie o replay. Ao alcançar a lacuna, confirme `HALTED`, razão `GAP_DETECTED`, indicação de novos fills bloqueados e ausência de execução no candle posterior.
5. Leia/revise a razão de `HALTED`, marque a confirmação e clique **Reset explícito**. Confira outro `replay_id` começando em READY com ledger inicial, enquanto o journal da sessão halted continua exportável.
6. Pause ou encerre um replay e clique **Comparar com backtest F3 no mesmo snapshot e intervalo**. Confira identidade/hash, janela, fees, qty, posição, equity e lista de diferenças; políticas do RiskGate são descritas separadamente.
7. Exporte journal JSON, equity CSV e relatório HTML. Refaça a conciliação `cash + base_qty × mark = equity` nos pontos exportados e confira `fees_paid_usdt` contra a soma de fills. O relatório classifica a execução como `PAPER_REPLAY` e não oferece compra/venda na exchange.

### Verificação focal registrada para F7

- `uv run python -m unittest discover -s tests -p test_paper_replay_f7.py -v` — verifica restart/reconciliação, event ID duplicado sem fill/fee duplicado, gap com halt, limite de exposição e sinal independente de barras posteriores ao relógio.
- `uv run python scripts/smoke_f7_replay.py` — pausa, reabre, reconcilia, retoma, compara no mesmo snapshot sintético e exporta journal/equity em diretório temporário.
- O roteiro manual continua necessário para conferir controles visuais e o comportamento no processo Streamlit; o smoke não acessa exchanges nem prova desempenho real.

## Roteiro manual — F8 / Paper Forward (Binance Spot BTCUSDT)

1. Confirme que o computador tem conexão pública à Internet e execute `uv sync`; não configure API key nem credencial de exchange.
2. Abra a UI com `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1` e selecione **Paper Forward**. Confirme os avisos `PAPER_FORWARD`, custos hipotéticos e licença de retenção/redistribuição `NÃO VERIFICADA`.
3. Clique **Iniciar PAPER_FORWARD** com a duração padrão. A tela inicia apenas `BTCUSDT` Spot público. Atualize status; espere ao menos uma vela 1m inteira iniciada depois da ativação e um aggTrade elegível após o delay.
4. Abra `artifacts/paper_forward/<run_id>/run.json`, `journal.jsonl`, `state.json` e `report.html`. Confirme origem WebSocket recente, sem arquivo histórico, `x=true`, `available_at` posterior ao receipt/evento de close, e `fill_at`/tempo do trade posterior ao `available_at`.
5. Confira que o venue/instrument ID são `BINANCE` / `BINANCE:SPOT:BTCUSDT`, existe no máximo um fill BUY, fee é contabilizada uma única vez, caixa/qty não são negativos e os hashes/sequências do journal encadeiam.
6. Para interromper antes do fim, clique **Parar sessão**; confirme evento `SESSION_ENDED` e artefatos encerrados. Para terminal, use `uv run quantlab paper-forward run --duration-seconds 180`; sem duração, `Ctrl+C` ou pedido de parada encerra a sessão.
7. Não interprete posição, equity, fill proxy, fee/slippage hipotéticos nem resultado desta sessão como lucro, execução garantida ou equivalência a fill real.

### Verificação focal registrada para F8

- `uv run python -m unittest discover -s tests -p test_paper_forward_f8.py -v` — valida candle fechado, temporalidade estrita do fill, sequence/gap, stale heartbeat e ledger.
- `uv run python scripts/smoke_f8_paper_forward.py --duration-seconds 150` — integração manual com feed público realmente recente; exige candle completo pós-ativação, um fill paper posterior e journal válido. Não usa daily archive nem endpoint privado.

