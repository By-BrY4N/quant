# QuantLab

QuantLab é um laboratório local de pesquisa. F0–F7 oferecem demo offline, aquisição Spot Binance com intervalos limitados, QA/snapshots, backtests, pesquisa, ML e replay histórico. F8 adiciona uma sessão PAPER_FORWARD opcional apenas para Binance Spot BTCUSDT, com feed público recente e ledger simulado, sem broker, API privada ou ordem real. Runs e relatórios ficam no computador. Licença de retenção/redistribuição do market data permanece não verificada; não há alegação de rentabilidade.

## Requisitos

- Windows PowerShell ou WSL 2.
- Python 3.12.
- `uv`.
- Dependências do painel (Streamlit, Plotly e pandas), instaladas por `uv sync`.

Instale `uv` se necessário. O método oficial WinGet no Windows é:

```powershell
winget install --id=astral-sh.uv -e
```

Feche e reabra o PowerShell após a instalação. No WSL:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Feche e reabra o terminal WSL após instalar. Outros métodos estão na [documentação oficial do uv](https://docs.astral.sh/uv/getting-started/installation/).

## Primeiro uso — Windows PowerShell

Na raiz do repositório:

```powershell
Set-Location 'D:\PBTech\laboratório'
uv sync
uv run quantlab --help
uv run quantlab demo
```

O comando `demo` imprime `RUN_ID` e o caminho do relatório. Abra o HTML mais recente:

```powershell
$run = Get-ChildItem .\artifacts -Directory | Sort-Object Name -Descending | Select-Object -First 1
Start-Process (Join-Path $run.FullName 'report.html')
```

## Primeiro uso — WSL

Na raiz Linux do clone (por exemplo, `~/src/quant`):

```bash
cd ~/src/quant
uv sync
uv run quantlab --help
uv run quantlab demo
```

Use o `RUN_ID` impresso pelo comando para abrir `artifacts/<RUN_ID>/report.html` no navegador. O relatório e o SVG são autocontidos; reabri-los não exige servidor nem rede.

## Verificação focal do ledger

```powershell
uv run python -m unittest discover -s tests -p 'test_ledger.py'
```

A fixture independente compra 1 unidade a 100,00 USDT e vende a 110,00 USDT, debitando 0,10% por lado em USDT, sem slippage. O resultado esperado é caixa de **1009,79 USDT**, taxas de **0,21 USDT** e posição final zero. Esse caso fixo não é o sizing de `BUY_HOLD`, que tenta usar o caixa disponível incluindo a fee.

## Arquivos do run

Cada execução cria `artifacts/<RUN_ID>/` com `run.json`, `fills.csv`, `equity.csv`, `metrics.json` e `report.html`. Nesta demo, CSV substitui Parquet para facilitar inspeção, sem adicionar dependência de armazenamento. A página inclui origem sintética, política de custos, curva de equity e fills. Os diretórios `artifacts/`, `state/`, `data/raw/`, `data/curated/` e `data/quarantine/` ficam fora do Git; a fixture pequena em `data/fixtures/`, se adicionada, continua versionável.

## Limites de F0

O motor cobre somente spot long-only, `CASH` e `BUY_HOLD`. Os preços, candles e curvas desta fase são sintéticos. Não entram nesta fase download Binance, SMA, otimização, ML, futuros, margem, short, paper forward ou execução em corretora.

## F1 — planejar, baixar ou importar um mês Binance

F1 aceita somente arquivos mensais de klines **BTCUSDT Spot 1m**, no máximo três meses fechados por comando. O plano não baixa nada; o tamanho remoto é mostrado como desconhecido porque não é consultado. A origem oficial publica arquivos `monthly` e um `.CHECKSUM` ao lado de cada ZIP; a documentação também registra que arquivos arquivados podem ser atualizados. [Binance Public Data](https://github.com/binance/binance-public-data)

### Exemplo manual: janeiro de 2024

Na raiz do repositório, no Windows PowerShell:

```powershell
uv run quantlab data plan --symbol BTCUSDT --interval 1m --start 2024-01 --end 2024-01
uv run quantlab data fetch --symbol BTCUSDT --interval 1m --start 2024-01 --end 2024-01
```

O primeiro comando exibe o nome do ZIP, os dois URLs oficiais, a contagem `1 ZIP + 1 CHECKSUM` e o espaço como desconhecido. O segundo baixa os dois arquivos com redirects limitados e revalidados, compara SHA-256 com o CHECKSUM, valida entrada única e CRC do ZIP e registra o resultado. Repetir `fetch` retorna `SKIPPED_ALREADY_VERIFIED` sem nova cópia; use `--refresh` somente para consultar a origem e detectar uma eventual revisão remota:

```powershell
uv run quantlab data fetch --symbol BTCUSDT --interval 1m --start 2024-01 --end 2024-01 --refresh
```

Para inspecionar os objetos locais sem consultar a rede:

```powershell
uv run quantlab data plan --symbol BTCUSDT --interval 1m --start 2024-01 --end 2024-01 --offline
```

Os originais ficam em `data/raw/binance/spot/monthly/klines/BTCUSDT/1m/AAAA-MM/<SHA256>/`, junto do CHECKSUM original (se fornecido) e de `manifest.json`. O catálogo `state/quantlab.sqlite` guarda origem, período, hash, bytes, ETag quando disponível, status, revisão e vínculo `supersedes`. Erros de HTTP, 404, 429 e tentativas limitadas também são registrados; 404 significa `MISSING_NOT_CONFIRMED`, nunca zero negociações.

### Importação offline do mesmo ZIP

Coloque na máquina sem rede o arquivo oficial com o nome original `BTCUSDT-1m-2024-01.zip` e, de preferência, o arquivo acompanhante `BTCUSDT-1m-2024-01.zip.CHECKSUM`. Então rode:

```powershell
uv run quantlab data import --zip 'D:\dados\BTCUSDT-1m-2024-01.zip' --checksum 'D:\dados\BTCUSDT-1m-2024-01.zip.CHECKSUM'
```

O import passa pelo mesmo limite de tamanho, allowlist do nome, checksum, validação de entrada, proteção ZIP Slip/ZIP bomb e CRC do fetch. Para um ZIP válido sem checksum disponível, omita `--checksum`: o manifesto registra `source_integrity=UNVERIFIED` e `zip_validation.status=PASS`; integridade do ZIP por CRC não comprova a origem. Se um checksum fornecido divergir, o arquivo é rejeitado e vai para `data/quarantine/`; uma cópia válida já registrada permanece intacta. Um CHECKSUM fornecido offline só prova correspondência com o hash escrito nele: para atribuí-lo à Binance, obtenha o CHECKSUM pelo canal oficial antes de ficar offline.

F1 apenas arquiva e verifica o ZIP original; não lê nem normaliza o conteúdo inteiro do lake. Não há futures, API key, endpoint privado ou ordem real.

## F2 — normalizar, auditar e fixar um snapshot

DuckDB é a única dependência de runtime acrescentada nesta fase. Ele grava e consulta os Parquet; o parser valida os 12 campos nativos e escolhe a unidade pelo mês do objeto: Spot até 2024-12 em milissegundos, desde 2025-01 em microssegundos. O parser não adivinha a unidade pelos dígitos do timestamp. O raw ZIP, CHECKSUM e manifest F1 permanecem intocados.

### Fluxo manual PowerShell — usar um mês conhecido

Após o `data fetch` F1 de `2024-01` acima, audite o objeto exato:

```powershell
uv run quantlab data audit --symbol BTCUSDT
```

Copie do resultado `RAW_OBJECT_ID` e os `partition_id` com `QA=PASS`. O comando mostra contagem observada/esperada, gaps, cobertura, agregados completos/bloqueados e os caminhos dos relatórios JSON/HTML. A primeira execução cria partições 1m, 1h e 4h; repetir é idempotente para a mesma versão e delay.

Crie o snapshot indicando cada partição desejada. Para pesquisa/backtest F3, inclua a partição 1m; a inclusão das agregações 1h/4h é opcional e também deve ser explícita:

```powershell
uv run quantlab data snapshot --partition-id <PARTITION_ID_1M> --partition-id <PARTITION_ID_1H> --partition-id <PARTITION_ID_4H>
```

A saída fornece `SNAPSHOT_ID`, hash do conteúdo, lista exata de partições e QA JSON/HTML. Se o raw foi importado offline, informe também a classificação de origem que você verificou, por exemplo `--data-origin REAL` ou `--data-origin SYNTHETIC`. Importações offline sem CHECKSUM validado ficam bloqueadas para snapshot; um CHECKSUM local só comprova correspondência com os bytes e não autentica sua origem.

### Regras que o QA exibe

- Cada minuto é UTC e representa `[open_at_utc, end_at_utc)`, com duração de 1 minuto; `source_close_time_raw` é preservado. `available_at_utc=end_at_utc+delay_assumed_ms`, por padrão 1.000 ms, com confiança `ASSUMED`.
- Gaps reduzem a cobertura explicitamente. Uma agregação 1h só aparece como PASS com os 60 instantes distintos; 4h exige 240. Janela incompleta é listada como `INCOMPLETE_BLOCKED`, sem ffill e sem vela parcial.
- Conflito de duplicata, checksum inválido/indisponível ou schema inválido gera quarentena de metadados e relatório. O ZIP raw não é movido nem alterado.
- `data snapshot` não resolve `latest`: exige uma ou mais `partition_id` exatas exibidas pelo audit, valida os hashes Parquet e raw, e não altera snapshots existentes.

O relatório marca a unidade temporal documentada para o mês, disponibilidade histórica assumida, proveniência do checksum e limitações de OHLCV. As velas são úteis para consulta e pesquisa, mas não representam spread, fila ou fills garantidos.

## F3 — backtest spot causal sobre snapshot

Use sempre o `SNAPSHOT_ID` exato impresso por `data snapshot`; a CLI não escolhe `latest`. O atraso inicial é herdado do manifesto F2 (1.000 ms no padrão). Fee e slippage são hipóteses configuráveis, não taxas oficiais da Binance.

```powershell
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h
```

O comando executa SMA cross com warm-up 20/50 e compara `CASH` e `BUY_HOLD` no mesmo recorte e política. Ele imprime o `RUN_ID`, localização do relatório HTML, nível da evidência (`IN_SAMPLE` para dados reais), gaps, janelas incompletas, warm-up descartado, vetos, custos e estado final de caixa/posição. O diretório `artifacts/backtests/<RUN_ID>/` contém `run.json`, `sources.json` (incluindo o manifesto do snapshot), `strategy.json`, `execution_policy.json`, `signals.csv`, `fills.csv`, `equity.csv`, `metrics.json`, `audit.json` e `report.html`.

F3 também oferece as regras representativas `MOMENTUM` e `MEAN_REVERSION`, em `15m`, `1h`, `4h` ou `1d`, sempre agregadas dos candles 1m completos do snapshot. Defaults: momentum compra quando o close supera o de 24 barras atrás; mean reversion usa z-score populacional trailing de 30 closes, incluindo a barra atual, entra em z ≤ -2 e sai em z ≥ 0. `BREAKOUT` compara o close com as máximas das 20 barras anteriores; `SMA_CROSS` usa 20/50. As posições são long-only, 0% ou 100% de exposição, sem alavancagem.

Exemplos para as novas regras:

```powershell
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy momentum --timeframe 1d --momentum-lookback 24
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy breakout --timeframe 15m --breakout-lookback 20
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy mean_reversion --timeframe 1h --mean-reversion-lookback 30 --mean-reversion-entry-z -2 --mean-reversion-exit-z 0
```

Além de retorno líquido, drawdown e exposição, os artefatos registram Sharpe diário, número de observações, win rate e profit factor. Sharpe usa retornos simples UTC em dias completos consecutivos, risco livre diário zero e anualização √365; fica indefinido com menos de duas observações ou variância zero. Win rate e profit factor usam P&L líquido de custos em trades fechados; campos sem denominador aplicável aparecem como `null`/`N/D`.

Exemplo de comparação configurada, sem alterar o snapshot:

```powershell
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h --fee-bps 0
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h --fee-bps 10
```

`--delay-ms` só deve ser usado quando se quer testar explicitamente outra hipótese de disponibilidade; o run marca a diferença em relação ao delay do snapshot. O fill usa o open do primeiro candle 1m estritamente posterior a `available_at`, com slippage adverso por lado e fee quote separada; não soma spread novamente. A posição final é marcada no close e não é vendida automaticamente. OHLCV é aproximação de fill, custos e latência não foram medidos, e resultados históricos `IN_SAMPLE` não garantem rentabilidade.

Para conferir o fluxo manualmente, abra `report.html`, inspecione as primeiras linhas de sinais e fills e todas as marcações de equity:

```powershell
Import-Csv .\artifacts\backtests\<RUN_ID>\signals.csv | Where-Object strategy -eq 'SMA_CROSS' | Select-Object -First 3
Import-Csv .\artifacts\backtests\<RUN_ID>\fills.csv | Where-Object strategy -eq 'SMA_CROSS'
Get-Content .\artifacts\backtests\<RUN_ID>\metrics.json
```

## F4 — painel Streamlit e primeiro uso M1

### Abrir o painel — Windows PowerShell

Na raiz do projeto:

```powershell
uv sync
uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1
```

Abra o endereço local impresso pelo Streamlit (por padrão, `http://127.0.0.1:8501`). O painel tem **Dados**, **Backtests**, **Operações** e **Runs anteriores**. Execute a partir da raiz para preservar os paths de catálogo e os metadados de run.

### Abrir o painel — WSL

Na raiz Linux do clone:

```bash
uv sync
uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1
```

### Caminho manual

1. **Dados → Executar demo sintética** cria um `RUN_ID` offline e o identifica em cada tela como `DADOS SINTÉTICOS`.
2. Para dados reais, envie o ZIP mensal oficial com nome `BTCUSDT-1m-AAAA-MM.zip` e o `.CHECKSUM`, se disponível. Sem checksum válido, o import fica `UNVERIFIED` e não pode sustentar um snapshot QA PASS.
3. Ou informe um mês fechado em **Preparar plano**. O app mostra as URLs e o estado local; o download só começa depois de marcar a confirmação explícita e clicar em **Baixar mês planejado**.
4. Selecione o objeto raw e clique **Auditar**. Revise cobertura, gaps e QA; depois selecione as `partition_id` exatas, incluindo 1m, para criar o snapshot.
5. Em **Backtests**, escolha símbolo e snapshot, timeframe 15m/1h/4h/1d, baseline `Cash`/`Buy & hold` ou regra `SMA`, `Breakout`, `Momentum` ou `Mean reversion`, parâmetros, capital, fee, slippage e atraso assumido. Clique **Executar backtest**.
6. Confira equity, benchmark buy & hold, drawdown e o aviso de custos hipotéticos. O `RUN_ID` e os artefatos são salvos em `artifacts/backtests/<RUN_ID>/`.
7. Em **Operações**, filtre por estratégia/período, selecione uma linha e confira horários, preço, quantidade, fee, veto e motivo. P&L realizado aparece na saída de um trade fechado; posição aberta continua marcada a mercado.
8. Em **Runs anteriores**, reabra o relatório HTML, proveniência e JSONs. Fechar o app não apaga `artifacts/`, `state/`, raw, curated ou snapshots.

O catálogo da interface é lido sem inicializar ou alterar um banco vazio. Erros resumidos aparecem no app; o traceback local fica em `state/streamlit.log`. O painel não executa código de configuração arbitrário nem carrega credenciais privadas. Fees e slippage não representam a tabela oficial da Binance, `available_at` é hipótese e o open 1m é proxy de fill.

Na F4/M1, comparação científica e walk-forward ainda estavam fora de escopo. A seção F5 abaixo registra a implementação M2. ML, DSR/PBO/White, paper replay/forward, futuros, short, alavancagem e ordens reais continuam fora de escopo.

## F5 — backfill limitado, comparação e pesquisa M2

F5 estende a origem pública Spot para BTCUSDT e ETHUSDT, com ZIPs diários ou mensais. O `plan` não baixa. O `fetch` requer comando explícito, intervalo fechado e teto agregado de transferência; são aceitos até 3 meses ou 31 dias por chamada. Monthly e daily são fontes alternativas: uma seleção de snapshot que inclua partições sobrepostas é recusada, e o manifest lista revisões/partições sobrepostas não selecionadas. Raw e snapshots continuam imutáveis.

### Planejar e baixar um intervalo pequeno

```powershell
uv run quantlab data plan --symbol BTCUSDT --granularity monthly --start 2024-02 --end 2024-02 --max-transfer-mb 128
uv run quantlab data fetch --symbol BTCUSDT --granularity monthly --start 2024-02 --end 2024-02 --max-transfer-mb 128
```

Para um mês parcial, prefira planejar dias fechados; o primeiro e último dia são inclusivos:

```powershell
uv run quantlab data plan --symbol ETHUSDT --granularity daily --start 2024-02-01 --end 2024-02-03 --max-transfer-mb 32
uv run quantlab data fetch --symbol ETHUSDT --granularity daily --start 2024-02-01 --end 2024-02-03 --max-transfer-mb 32
```

O limite conta os bytes de payload ZIP/CHECKSUM, inclusive tentativas repetidas. Se a chamada interromper por erro ou teto, ela para sem continuar para os ZIPs seguintes; os objetos já verificados permanecem no catálogo. O tamanho remoto não é consultado pelo plano. O limite por arquivo é 64 MiB para o ZIP e 16 KiB para o CHECKSUM. Backfills extensos exigem autorização explícita, revisão da capacidade e uma finalidade de pesquisa registrada; dados devem ser auditados antes de qualquer backtest.

Depois do fetch/import, audite o raw, escolha as partições `1m` QA PASS e fixe cada `partition_id` explicitamente. Não misture mensal/diário ou revisões com janelas sobrepostas no mesmo snapshot. O manifest registra granularidade, arquivo, revisão e exclusões; cobertura e lacunas por símbolo ficam visíveis na tabela de fontes.

### Comparar runs

No painel, abra **Comparar** e selecione de dois a quatro runs F3/F5. A comparação mostra retorno líquido, drawdown, exposição, turnover, fees e slippage. Se instrumento, intervalo de cobertura, snapshot ou política de execução divergir, um aviso vermelho informa as dimensões incompatíveis; as linhas permanecem visíveis e não são apresentadas como períodos iguais.

### Pré-registrar uma pesquisa

Em **Pesquisa**, selecione um snapshot de desenvolvimento QA PASS, regra F3, custos, orçamento de tentativas, parâmetros permitidos, métrica/direção/limiar primário, tamanho de treino/teste, folds e período final. O holdout precisa começar depois do fim coberto pelo desenvolvimento, usar o mesmo instrumento, cobrir integralmente o período e conter warm-up causal. O snapshot final pode ser vinculado enquanto permanece selado. Cada trial salva `artifacts/research/<family>-vN/<trial-id>/trial.json` e `report.html`, além do run F3 reprodutível.

O walk-forward é cronológico, sem shuffle; treino expande e os testes seguintes são períodos disjuntos. Métricas por fold incluem retorno, drawdown, Sharpe diário, win rate, profit factor, exposição, turnover, custos e stress de fee/slippage 2x e 3x. UI/CLI registram `SMA_CROSS`, `BREAKOUT`, `MOMENTUM` e `MEAN_REVERSION` nos quatro timeframes F3; parâmetros variáveis aceitos são lookbacks inteiros pré-registrados, e limites z de mean reversion permanecem congelados por versão. UI/CLI aceitam registrar horizon e purge para uso futuro, mas esses campos não alteram folds F5 porque as regras atuais não têm labels futuros. Se houver menos folds de teste válidos que o mínimo pré-registrado, o estado é `INCONCLUSIVO`; o critério primário registrado produz `NO_GO` ou `CANDIDATO_A_PAPER`. O rótulo não é autorização para capital real nem promessa de vantagem. Parâmetros de estratégia são escolhidos manualmente a partir dos valores pré-registrados; F5 não implementa otimizador, DSR, PBO ou White.

Roteiro pela interface:

1. Abra **Dados** e confira as fontes/snapshots por símbolo, granularidade, revisão, cobertura e gaps.
2. Em **Pesquisa**, registre família/versão, regra, valores permitidos, métrica primária e corte temporal antes de executar qualquer trial.
3. Execute um ou poucos trials; compare seus folds e os custos 2x/3x. O holdout segue selado.
4. Vincule o snapshot separado que cobre o período final. A janela final deve ser posterior ao fim do snapshot de desenvolvimento.
5. Escolha um trial completo e congele a versão. O congelamento verifica que o snapshot final também fornece o warm-up do trial escolhido.
6. Confira os hashes/artefatos; marque a confirmação e abra a avaliação final uma única vez. A interface grava `HOLDOUT_OPENED`; novas tentativas e edições dessa versão ficam bloqueadas mesmo se a avaliação falhar.
7. Em **Comparar**, compare o run formal com runs compatíveis. Se os períodos/snapshots forem diferentes, confira o aviso e os metadados antes de interpretar.

Reprodução focal e smoke offline usados na fase:

```powershell
uv run python -m unittest discover -s tests -p test_research_f5.py -v
uv run python scripts/smoke_f5_ui.py
```

O teste cria ZIPs sintéticos curtos, exercita cobertura mensal/diária sobreposta, uma partição treino/teste, custos 2x/3x, campos horizon/purge informativos e abertura única do holdout. O smoke abre as seis páginas com AppTest, compara dois runs sintéticos compatíveis, confirma o aviso ao comparar períodos/snapshots diferentes e prepara um plano ETH diário de um ZIP com teto de 2 MiB, sem fetch. Para abrir o painel manualmente, use `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`. A pesquisa real inicial de 29/10/2026 registra em `docs/RELATORIO_PESQUISA_INICIAL_2026-10-29.md` a cobertura F2 contínua até junho, o raw F1 adicional de julho a outubro mantido fora dos backtests e os limites da evidência calculada.

## F6 — experimento supervisionado causal

F6 acrescenta a página **ML** e o comando `quantlab ml run`. A tarefa única é classificação binária: o retorno simples entre o close da barra atual e o close de `H` barras futuras supera uma barreira aproximada de custos round-trip? O padrão é `H=6` barras de 1h; timeframe e H são configuráveis. `label_start_at` é o fim da barra atual; `label_end_at` é o fim da barra H à frente; o label só é observável em `label_end_at + delay_assumed`.

As features são retornos de 1/3/6 barras, volatilidade e range trailing de 12 barras, distância da SMA20 e volume relativo trailing de 20 barras. Features só usam barras completas e janelas passadas. A primeira parte cronológica da validação calibra thresholds; labels do treino cujo `label_observable_at` cruza a validação são removidos. Imputer, scaler e regressão logística ajustam somente no treino. O experimento registra oito candidatos de threshold, inclui controle nulo, logística preço-only, logística preço+volume e baseline SMA.

Para usar no painel, abra **ML**, escolha snapshot QA PASS que não esteja selado como holdout, e confira os intervalos UTC sugeridos antes de executar. Para usar a CLI:

```powershell
uv run quantlab ml run `
  --snapshot <SNAPSHOT_ID> `
  --train-start 2024-01-01T00:00:00Z `
  --train-end 2024-01-15T00:00:00Z `
  --validation-start 2024-01-15T00:00:00Z `
  --validation-end 2024-02-01T00:00:00Z `
  --timeframe 1h `
  --horizon 6
```

Os timestamps são exemplos; escolha períodos com cobertura suficiente no snapshot. A UI e a CLI recusam holdout F5 selado. A avaliação econômica ocorre numa subjanela posterior à calibração, com labels de avaliação usados somente para métricas, depois de gerar as previsões.

Cada execução grava `artifacts/ml/<experiment_id>/experiment.json`, `threshold_trials.json`, `predictions.csv` e `report.html`, além do run F6 em `artifacts/backtests/<RUN_ID>/`. O CSV expõe `pred_at`, `feature_at`, `available_at`, `label_start`, `label_end`, `label_observable_at`, features e papel da amostra. O manifest registra fold, contagem do purge, disponibilidade máxima dos labels de treino, métricas Brier/log loss/curva de calibração e resultado econômico de modelo nulo, modelos lineares, SMA, cash e buy & hold.

O resultado econômico de ML usa os mesmos sinais temporais, fill no primeiro open 1m elegível, fee, slippage e ledger spot da engine F3. Se nenhuma variante logística superar simultaneamente o controle nulo e o SMA em retorno líquido na janela avaliada, o relatório mostra **SEM GANHO INCREMENTAL**. Se superar, isso ainda é uma observação exploratória de validação, não evidência independente de edge. A barreira do label é aproximada; os custos efetivos continuam vindo do motor. Um único fold, um instrumento e a calibração dentro da validação limitam a evidência. Acurácia não implica lucro, e o circuito não emite ordens.

### Verificações focadas de F6

```powershell
uv run python -m unittest discover -s tests -p test_ml_f6.py -v
uv run python scripts/smoke_f6_ml.py
```

O teste altera valores sintéticos no fim do dataset e verifica que probabilidades anteriores permanecem iguais. O smoke executa treino → predição → backtest F3 → export de JSON/CSV/HTML em pasta temporária, sem consultar fontes externas.

## F7 — Paper Replay histórico offline

A página **Paper Replay** acompanha uma regra F3 (`CASH`, `BUY_HOLD`, `SMA_CROSS` ou `BREAKOUT`) em candles históricos de 1m, em ordem cronológica. Ao iniciar, o snapshot QA PASS e a configuração recebem hashes congelados. A página avança um lote limitado por atualização; não usa `sleep` longo nem loop infinito no render. Pausar, retomar, parar e resetar afetam somente a sessão local.

Os fills reutilizam a política F3: open do primeiro candle 1m estritamente posterior a `available_at`, taxa em quote e slippage proxy por lado no ledger comum. RiskGate recusa exposição acima do limite; drawdown máximo, QA essencial inválido, saldo negativo, lacuna 1m, barra elegível ausente, evento duplicado ou divergência de ledger podem bloquear novos fills ou levar a `HALTED`. Um reset de sessão encerrada ou halted cria outro `replay_id` e preserva o journal anterior; para `HALTED`, marque a revisão do motivo.

Para usar:

1. Execute `uv run streamlit run src/quantlab/ui/app.py --server.address 127.0.0.1`.
2. Em **Paper Replay**, selecione um snapshot QA PASS que não esteja selado como holdout. Escolha uma regra F3, timeframe, custos, exposição máxima e drawdown máximo.
3. Clique **Congelar e iniciar replay**. A configuração e o hash do snapshot ficam fixos para esta sessão.
4. Confira o relógio UTC, caixa, posição marcada, equity, drawdown e journal. **Pausar** move a sessão para `READY`; após reabrir o app, selecione a sessão e clique **Retomar**. O app reexecuta os fills registrados para reconciliar caixa, posição, fees e equity antes da retomada.
5. Se surgir uma lacuna, a sessão passa para `HALTED`, registra a causa e não processa a barra posterior. Um reset revisado começa nova sessão desde o início e mantém a anterior.
6. Pause ou encerre para exportar o journal JSON, equity CSV e relatório HTML autocontido. Compare com F3 no mesmo snapshot e intervalo reproduzido; a tela e o HTML listam diferenças e políticas específicas do RiskGate.

Toda execução e exportação usa o rótulo `PAPER_REPLAY`. É um replay histórico offline, nunca `PAPER_FORWARD`; a disponibilidade histórica continua `ASSUMED`, custos são hipóteses e preços OHLCV são proxies. F7 aceita somente regras embutidas F3; previsões ML F6 pré-computadas não são aceitas como estratégia causal de replay. Não há chaves privadas, socket de exchange nem compra/venda real.

### Verificações focadas de F7

```powershell
uv run python -m unittest discover -s tests -p test_paper_replay_f7.py -v
uv run python scripts/smoke_f7_replay.py
```

O teste focal cobre restart/reconciliação sem segundo fill ou fee, colisão de event ID, bloqueio de exposição, gap e invariância de sinal a candles posteriores ao relógio. O smoke aciona a UI para iniciar/pausar/retomar, compara com F3, exporta journal/equity/HTML e abre outra sessão sintética com gap para confirmar `HALTED`.
