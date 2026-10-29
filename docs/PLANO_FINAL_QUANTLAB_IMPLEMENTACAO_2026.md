# QUANTLAB — PLANO FINAL DE IMPLEMENTAÇÃO ORIENTADO A ENTREGA

**Versão:** 2.0 | **Data-base:** 16/10/2026 | **Idioma:** pt-BR  
**Origem:** revisão integral de `PLANO_MESTRE_IMPLEMENTACAO_TRADING_QUANT_2026(1).md` (2.241 linhas).  
**Status:** especificação de construção, **não software construído**, **não evidência de rentabilidade**.  
**Arquivos complementares:** `PROMPTS_CODEX_QUANTLAB_GPT6_LUNA.md` (prompts prontos para execução por etapas).

> **Mandato de produto:** construir primeiro um laboratório local que uma pessoa consiga **abrir, importar dados, rodar um backtest, inspecionar trades, comparar alternativas e exportar um relatório**. Depois torná-lo cientificamente mais robusto. Nunca sacrificar causalidade, segurança financeira e contabilidade para apresentar gráficos bonitos.

---

## 0. Leitura executiva: a decisão central

O plano v1 é forte como **catálogo de requisitos e protocolo de pesquisa**, mas fraco como ordem de construção por agentes: une 14 épicos, dezenas de integrações, duas engines, estatística avançada, event-driven, ML e paper trading antes de tornar obrigatório um circuito visual completo para o usuário. A v2 conserva a ambição, mas muda a sequência para **fatias verticais** e para uma **única semântica de resultados**.

### Resultado concreto desejado

No primeiro marco utilizável, o usuário poderá:

1. Rodar uma demonstração offline preinstalada (claramente marcada **SINTÉTICA**).
2. Baixar/registrar um mês fechado de BTCUSDT spot 1m da Binance (ou importar o mesmo ZIP já baixado).
3. Ver cobertura, lacunas e proveniência; bloquear datasets reprovados.
4. Agregar 1m para 1h/4h; escolher `cash`, `buy_hold`, `SMA` ou `breakout`.
5. Executar backtest de spot, **long-only e sem alavancagem**, com caixa, taxa e slippage explícitos.
6. Abrir um painel local com equity, drawdown, operações, custos, configurações e advertências.
7. Exportar um diretório de execução reprodutível e relatório HTML sem precisar aprender SQL.

**Isso é o MVP demonstrável (M1), não uma tese lucrativa comprovada.** Walk-forward, ML e paper forward são entregas posteriores sobre o mesmo núcleo.

### O que não entra no caminho crítico

NautilusTrader, Freqtrade, vectorbt, RL, deep learning, multiagentes *dentro do produto*, L2/L3, funding, futuros, múltiplas exchanges, macro/ALFRED, notícias, scraping de redes sociais, 1.000 indicadores, cloud, microserviços, Docker obrigatório, PostgreSQL, CI complexo e sistemas de ordens reais. São **opções futuras condicionais**, não obrigações disfarçadas do MVP.

---

# PARTE I — AUDITORIA CRÍTICA DA V1

## 1. O que está certo e deve ser preservado

| Classificação | Tema na v1 | Veredito e decisão para a v2 |
|---|---|---|
| **CORRETO/ESSENCIAL** | Raw imutável, SHA-256, proveniência, versões | Preservar. O hash local sozinho não substitui o `.CHECKSUM` publicado pela origem. |
| **CORRETO/ESSENCIAL** | Dados disponíveis no instante da decisão (`available_at`) | Preservar; separar **timestamp de mercado**, **fim de candle**, **disponibilidade assumida** e **horário do fill**. |
| **CORRETO/ESSENCIAL** | Não selecionar modelos pelo holdout final | Preservar. O usuário não deve ver resultados do holdout durante tuning. |
| **CORRETO/ESSENCIAL** | Comparar cash/buy-and-hold e custos líquidos | Preservar; adicionar comparação de **exposição** e risco equivalente mais tarde. |
| **CORRETO/ESSENCIAL** | 1m como fonte canônica para BTC/ETH, 1h/4h para decisão | Preservar por baixo custo e flexibilidade, com validação de gaps e agregação. |
| **CORRETO/ESSENCIAL** | Parquet + DuckDB + SQLite no ambiente local | Preservar: simples, adequados a batch e reprodutíveis. |
| **CORRETO/ESSENCIAL** | Hipótese nula, controles, walk-forward, ablações | Preservar por etapas, sem transformar o MVP em pacote estatístico universitário. |
| **CORRETO/ESSENCIAL** | Sem ordens reais na v1 | Preservar por construção: não haverá API privada, assinatura nem adaptador de execução real. |
| **CORRETO/ESSENCIAL** | Previsão ≠ decisão ≠ ordem ≠ fill ≠ posição | Preservar em nomes/tipos separados, inclusive no simulador simples. |
| **CORRETO/ESSENCIAL** | Dados adicionais exigem hipótese e teste incremental | Preservar. Custo de manutenção costuma dominar a simples obtenção de um CSV. |

## 2. O que está excessivo, impreciso ou tecnicamente problemático

**P0 = corrigir antes de usar; P1 = corrigir na próxima fase; P2 = reclassificar como evolução.** Referências abaixo indicam seções/linhas da v1, para auditoria de origem.

| ID | Gravidade | Referência v1 | Diagnóstico | Correção obrigatória |
|---|---|---|---|---|
| R01 | **P0 / escopo** | §3–5, §19; linhas 193–245, 1124–1360 | W0 inclui PoCs Freqtrade e Nautilus, enquanto W3/W4 só depois entregam o primeiro backtest. Isso atrasa um produto visível. | Começar por **demo offline → backtest canônico → UI → downloader**, com dependências reais. |
| R02 | **P0 / risco financeiro** | §13.2; linhas 785–790 | “Próximo open após fechar candle” pode ser temporalmente impossível se `available_at` contém latência positiva. | Regra executável indexada à barra de 1m **posterior** à disponibilidade; nunca preencher retroativamente o open já passado. |
| R03 | **P0 / dados** | §7, §9, §10; linhas 360–433, 547–679 | `available_at` histórico é conceito útil, mas a origem OHLCV não mede a latência real do trader. | Registrar `available_at_policy`, `assumed_delay_ms` e `confidence=ASSUMED`; backtest não pode reivindicar PIT empírico da feed. |
| R04 | **P0 / schema** | §9.3; linhas 594–627 | Downloader didático não implementa checksum oficial, proteção suficiente contra ZIPs adversos, tratamento de revisões e transação do catálogo. | Tratar snippet como **ilustração, não código pronto**. Implementar allowlist, `.part`, hash da fonte, ZIP limitado e commit idempotente. |
| R05 | **P0 / integridade** | §8.2; linhas 478–527 | `raw_objects`/`partitions` tem modelo básico, mas faltam ligações explícitas entre revisões, *datasets* compostos, decisões e versões de estratégias. | IDs imutáveis + manifest de snapshot + `run.json` apontando para objetos exatos; revisar o schema à medida que a implementação avance. |
| R06 | **P0 / execução** | §13.4; linhas 814–828 | Bps “all-in por round trip” podem ser aplicados duas vezes se também forem debitados maker/taker/spread. | Separar taxas por lado e **proxy adicional** de execução; documentar se cada campo já inclui spread. |
| R07 | **P0 / backtest** | §13.7, §19 EP07 | Comparar vários motores desde cedo pode criar três resultados supostamente canônicos com semânticas distintas. | **Uma engine candle/eventos simples como fonte da verdade**; comparações com frameworks apenas após estabilizar casos manuais. |
| R08 | **P0 / experiência** | §3.1, §16.6; linhas 55–65, 1037–1042 | UI é adiada até a rastreabilidade total, contrariando a meta de ser testável manualmente. | UI **read-only** em M1, alimentada por artefatos reais (não mocks travestidos). |
| R09 | **P0 / dados** | §6.1, §7.4; linhas 261–264, 398–415 | Precisão ms/us está correta como regra geral para arquivos **spot** Binance desde 01/01/2025, mas não se deve extrapolar para futuros/APIs. | Parser por `source + market + arquivo + período + schema`; rejeitar unidade inconsistente. |
| R10 | **P1 / qualidade** | §10.2; linha 655 | Deduplicação pode esconder revisões ou timestamps conflituosos. | Duplicata idêntica pode ser resolvida com log; **conflito** gera quarentena/revisão, nunca escolha silenciosa. |
| R11 | **P1 / ciência** | §14.6; linhas 908–924 | “Rejeitar se não supera B&H” é uma política estreita: portfólios de menor drawdown podem atender a objetivos diferentes. | Critério primário **pré-registrado**; comparar retorno, risco, exposição, turnover e cash. Nunca mudar métrica após ver OOS. |
| R12 | **P1 / ciência** | §14.4, EP08.08–09 | DSR, PBO/CSCV, White e FDR todos no fluxo inicial são caros e exigem premissas próprias. | Primeira entrega: walk-forward, custos, logs, análises por período, IC opcional; métodos sofisticados só com dados/desenho apropriados. |
| R13 | **P1 / ML** | §15.3; linhas 946–960 | Pseudocódigo de folds não é suficiente para garantir labels não sobrepostas, *purge*, calibração e normalização corretos. | Definir timestamps do alvo e limites do fold; pipeline scikit-learn fit apenas treino; validação interna para thresholds. |
| R14 | **P1 / paper** | §11, §16, EP11 | Backfill diário da Binance não é transmissão em tempo real. “Paper” poderia virar replay disfarçado. | Nomear **paper replay offline** primeiro; **paper forward** só com ingestão incremental real, relógio e estado. |
| R15 | **P1 / fonte** | §6.3 | Usar OHLCV corrigido/substituído na origem pode reescrever a história analisada. | Arquivar revisões e distinguir “história reconstruída por fonte” de “informação publicada na época”. |
| R16 | **P1 / medição** | §18.1 | Volume estimado de 1m é plausível, mas custo/RAM depende de implementação; testes de benchmark longo não validam ciência. | Primeiro medir 1 mês real, depois 1 ano; não baixar década inteira no setup inicial. |
| R17 | **P1 / testes** | §10 e §19 | Grande suíte de testes, chaos tests, benchmarks e CI pode consumir a maior parte do tempo do Codex. | **Orçamento de verificação por fase:** 1 smoke end-to-end + poucos testes *críticos* + checklist manual. |
| R18 | **P1 / manutenção** | §3.4 e anexo | Python 3.13 + stack específica não precisa ser decisão inegociável; algumas libs mudam suporte. | Preferir Python 3.12 inicialmente e **resolver/travar** versões compatíveis no dia do build. |
| R19 | **P1 / período** | §14.2, §20 | Split 2020–2023/2024–2025/2026-01–09 é ilustrativo, não serve se disponibilidade e cobertura forem insuficientes. | Validar cobertura ANTES de selar holdout; definir por símbolo e registrar exceções. |
| R20 | **P1 / segurança** | §9.3 | Redirecionamento automático de `httpx` e `Retry-After` parcial não bastam para política de hosts e retries. | Verificação de **cada redirect**, limites finitos, status explícito, validação de hash e falha fechada. |
| R21 | **P1 / semântica** | §13.3; linhas 793–812 | Modelo completo de order intent/partial fills/margin/limit é prematuro para sinais de 1h/4h e spot. | Spot market simulada + rejeição de saldo/lote primeiro; limit/partial/cancel tornam-se etapa separada. |
| R22 | **P2 / arquitetura** | §4 | Freqtrade e Nautilus são úteis, mas uso conjunto é opcional, não condição de aceitabilidade. | Manter adaptadores possíveis sem dependências instaladas por padrão. |
| R23 | **P2 / apêndice** | Anexo A, §6 | A matriz do anexo fala em spot+perp “imediatamente”, enquanto contrato principal limita a spot. | **Plano v2 prevalece:** spot no MVP; perp apenas após módulo próprio de contrato/funding/margem. |
| R24 | **P2 / detalhismo** | §19 | Centenas de issues e gates criam sensação de progresso sem uma unidade verificável de valor. | Backlog por **marcos completos**, com entregáveis observáveis e no máximo 1–2 workstreams simultâneos. |
| R25 | **P2 / nomenclatura** | §14–15 | Código/relatório usa “OOS”, “GO” ou “validado” sem especificar nível de evidência. | Etiquetas obrigatórias: `DEMO`, `IN_SAMPLE`, `WALK_FORWARD`, `SEALED_HOLDOUT`, `PAPER_REPLAY`, `PAPER_FORWARD`. |

### Correções factuais confirmadas em fontes oficiais

- **Binance Public Data:** arquivos `daily`/`monthly`, arquivo acompanhante `.CHECKSUM`, alterações históricas do acervo e **microssegundos para SPOT desde 01/01/2025**. Logo, o plano estava certo no ponto importante, mas o código precisa refletir a precisão sem adivinhação. Fonte: <https://github.com/binance/binance-public-data>.
- **vectorbt:** distribuição comunitária **Apache 2.0 com Commons Clause**, isto **não equivale** a dizer “Apache puro e comercialização irrestrita”. Fonte: <https://vectorbt.dev/terms/>.
- **DuckDB:** `ASOF LEFT JOIN` da v1 é sintaticamente legítimo; o risco está em *revisões, validade, empate e confiança temporal*, não no operador SQL em si. Fonte: <https://duckdb.org/docs/current/sql/query_syntax/from>.
- **Freqtrade:** disponibiliza `lookahead-analysis`; é recurso útil de verificação, mas não demonstra por si só execução realista nem ausência universal de vazamento. Fonte: <https://docs.freqtrade.io/en/stable/lookahead-analysis/>.
- **Codex/modelos:** `high` e `xhigh` são níveis de esforço, quando oferecidos pelo modelo/ambiente; a capacidade de criar agentes é **dependente da ferramenta/sessão**, não surge porque o prompt diz “crie agentes”. Fontes: <https://developers.openai.com/api/docs/guides/deployment-checklist> e <https://developers.openai.com/api/docs/guides/agents-api/multi-agent>.

### O que **não** foi verificado nesta revisão

Não houve download/análise empírica de meses reais, benchmark de hardware, backtest funcional, auditoria contratual de market data, aferição de taxas/spread da conta, inspeção do código fonte de todas as versões das bibliotecas, nem certificação da alegada lucratividade. Datas, preços de fornecedores e versões listados no **Anexo A** da v1 continuam fotografias da pesquisa anterior, não recomendações para instalar/contratar agora.

---

# PARTE II — CONTRATO DE PRODUTO E ARQUITETURA

## 3. Requisitos travados

### 3.1 Escopo M1 obrigatório

- Usuário único, ambiente local, língua portuguesa no painel/documentação.
- BTCUSDT spot primeiro; ETHUSDT entra após comprovar o circuito BTC.
- Velas base 1m; estratégias em 1h e 4h; `15m`/`1d` para visualização posterior.
- Sem conta na exchange, sem API key privada, sem ordens reais, sem shorts, sem alavancagem, sem margem.
- Dados offline sintéticos embutidos para iniciar mesmo sem internet; **não** usar estes dados como prova de estratégia.
- Datasets externos baixados apenas sob ação explícita, período acotado, com pré-visualização e estimativa aproximada de arquivos.
- Painel Streamlit com no mínimo três abas: **Dados**, **Backtests**, **Operações**; acrescentar **Comparar** na fase M2.
- Configurações persistidas e histórico de runs; valores nunca simplesmente hardcoded na UI.
- Backtest *candle-based* **conservador e explicável**; mercado spot, ordens simuladas a mercado, taxa e custo de execução registrados separadamente.
- Validação manual via fixture verificável e um caso numérico conhecido.

### 3.2 M2 (pesquisa confiável)

- Histórico completo 2020–2026-09 **quando a cobertura permitir**, sincronização incremental e snapshots.
- BTC+ETH; SMA, breakout, reversão; comparador e evolução de desempenho.
- Walk-forward temporal, treino/validação/holdout selado, estresse de custo, sensibilidade de parâmetros.
- Sharpe, drawdown, exposição, turnover, expectativa, benchmark, contagem de trials e nível de evidência.
- Relatório HTML/JSON com hipóteses, limitações, motivos de veto.

### 3.3 M3 (ML e paper replay)

- Modelo linear/logístico primeiro; um boosting opcional se houver baseline e budget.
- Features causais e pipeline temporal; comparação incremental contra regras simples.
- Replay sequencial offline da estratégia congelada, ledger retomável, decisões e risco em modo paper.

### 3.4 M4+ (somente demanda justificada)

- Paper forward com coleta realmente recente, monitoramento e reconciliação; dois provedores/venues; perp com funding/margem/mark; livro L1/L2; notícias/macro PIT; NautilusTrader como opção de replay sofisticado; análises estatísticas especializadas. Não há prazo implícito de implementar tudo.

## 4. Stack de referência, com dependências deliberadamente limitadas

| Camada | Escolha preferida | Justificativa e regra |
|---|---|---|
| Linguagem | Python **3.12** inicialmente | Ecossistema maduro; escolher versão pontual via lock, sem exigir upgrade arbitrário de SO. |
| Ambiente | `uv`, `pyproject.toml`, `uv.lock` | Instalação e execução reprodutíveis (`uv sync`, `uv run`). |
| CLI | Typer ou argparse (escolher **um**) | Rotinas determinísticas, fáceis de executar no Windows/WSL. |
| HTTP | HTTPX | Download streaming com limites explícitos. |
| ETL | Polars + PyArrow | CSV → Parquet, agregações; usar recursos sob demanda. |
| Consultas | DuckDB | Consulta local sobre Parquet, sem servidor. |
| Metadados | SQLite | Objetos, partições, snapshots, runs, erros, revisões. |
| Configuração | Pydantic + TOML/YAML, ou dataclasses | Escolher um esquema estável; não combinar três validadores. |
| UI | Streamlit + Plotly | Interface local simples; somente lê resultados e dispara funções da aplicação existentes. |
| ML futuro | scikit-learn e **um** booster opcional | Nenhum framework de IA necessário no MVP. |
| Testes | pytest + ruff | Poucos testes focados no núcleo; nenhum teste de carga gratuito e repetitivo. |
| Versionamento | Git + hashes de dados | Dados grandes e resultados ficam fora de Git, exceto pequenas fixtures. |

**Compatibilidade:** escolher versões travadas que se instalem no computador real. Se uma biblioteca causar impedimento sério, trocar **somente aquela dependência**, registrar em `docs/DECISIONS.md` e seguir. Não montar containers e pipeline de CI como pré-requisito para o primeiro uso; WSL2 é recomendado caso o Windows nativo dificulte I/O, mas **não obrigatório**.

## 5. Estrutura física definitiva

```text
quantlab/
├── AGENTS.md                         # instruções curtas e estáveis aos agentes Codex
├── README.md                         # instalação e primeiro uso manual
├── pyproject.toml
├── uv.lock
├── .env.example                      # sem chaves reais
├── configs/
│   ├── app.toml
│   ├── instruments.toml
│   ├── strategies/
│   ├── experiments/
│   └── execution_costs.toml
├── src/quantlab/
│   ├── cli.py
│   ├── settings.py
│   ├── domain/                       # timestamps, instrumentos, ordens, fills
│   ├── data/                         # fetch, parser, manifests, QA, agregação
│   ├── catalog/                      # SQLite, versionamentos e snapshots
│   ├── strategies/                   # cash, buyhold, SMA, breakout, reversal
│   ├── simulation/                   # relógio, fills a mercado, ledger, custos
│   ├── research/                     # splits, walk-forward, features, métricas
│   ├── ml/                           # somente M3
│   ├── paper/                        # somente M3/M4
│   ├── reports/                      # HTML, JSON, arquivos auditáveis
│   └── ui/                           # Streamlit, sem lógica duplicada
├── data/
│   ├── raw/                           # ZIP + CHECKSUM; conteúdo imutável
│   ├── curated/                       # Parquet versionado
│   ├── quarantine/
│   └── fixtures/                       # arquivos pequenos, SINTÉTICOS
├── state/                             # quantlab.sqlite; fora do Git
├── artifacts/
│   └── <run_id>/
├── tests/
│   ├── test_timestamps.py
│   ├── test_ledger.py
│   ├── test_no_lookahead.py
│   └── test_ingestion_integrity.py
└── docs/
    ├── DECISIONS.md
    ├── DATA_POLICY.md
    ├── MANUAL_QA.md
    ├── CHANGELOG_IMPL.md
    └── RUNBOOK.md
```

**Regra:** não criar módulos vazios apenas para “respeitar arquitetura”. `src/quantlab/...` pode crescer progressivamente. Tudo deve ser chamável tanto pela CLI quanto pelo painel sem copiar regras para cada lugar.

## 6. Modelo funcional de dados e contratos

### 6.1 Identificador de instrumento

Formato inicial: `BINANCE:SPOT:BTCUSDT`, `BINANCE:SPOT:ETHUSDT`. Semântica: `venue`, `market_type`, `native_symbol`, `base`, `quote`. Não reutilizar o mesmo ID para perp ou outra exchange. Especificações históricas de `tick_size`, `step_size` e `min_notional` podem não estar recuperáveis integralmente: **configuração atual/ilustrativa não é apresentada como histórica**.

### 6.2 Time model obrigatório

- `open_at_utc`: início de um minuto/barra.
- `end_at_utc`: fim exclusivo do intervalo (`[open,end)`).
- `source_close_at`: campo original da exchange, preservado.
- `available_at_utc`: fim + atraso assumido no backtest de candle; metadado expresso como hipótese, não medição real.
- `decision_at_utc`: instante após o dado estar disponível e a regra poder decidir.
- `execution_at_utc`: início da primeira barra 1m permitida pela política de execução.
- `downloaded_at_utc`: **hoje**, não o horário de conhecimento histórico.
- `source_revision`, `parser_version`, `dataset_id` e `snapshot_id`.

Para o MVP, usar **somente candles FECHADOS** e **regra explícita de execução**:

1. Calcular sinal a partir da barra de decisão 1h/4h terminada em `T`.
2. Assumir `available_at = T + delay_assumed`, com `delay_assumed > 0`, por padrão 1 segundo (hipótese ilustrativa ajustável).
3. Execução spot a mercado somente no **primeiro open de candle 1m estritamente posterior a `available_at`**, desde que os dados daquela barra existam e não haja gap. Ex.: 1h encerra 15:00, disponível 15:00:01, pode executar em **15:01**, não em 15:00.
4. Preço: `open_1m[execution_at]` modificado pelo modelo explícito de custo/slippage da operação. A hora/preço de abertura é **proxy de fill**, nunca promessa de fill real a esse valor.
5. Nenhuma decisão/execução deve consultar o high/low/close do candle de execução nem candles seguintes. Se faltar preço ou especificação essencial, **não preencher**, sinalizar evento faltante.
6. A estratégia mantém exposição até o próximo sinal; todos os fills, inclusive os vetados por saldo/risco, são rastreados.

Ao introduzir dados bid/ask ou book, outra política de execução pode ser adicionada, versionada e **não silenciosamente substituída**.

### 6.3 Binance: parser e aquisição corretos

- Fonte oficial: `https://data.binance.vision`, somente caminhos permitidos `/data/spot/monthly/klines/{SYMBOL}/1m/...` (diário após M1).
- Baixar ZIP e `<arquivo>.zip.CHECKSUM`; parsear conteúdo do arquivo CHECKSUM com validação; comparar SHA-256 do ZIP.
- Preservar ZIP bruto e checksum original; logar URL, ETag (se houver), content length, data do download, hash, versão do parser e estado.
- Arquivos podem conter CSV sem header ou formatos com diferença de cabeçalho; **validar colunas e formato em vez de assumir a primeira linha**. O kline spot oficial possui 12 campos: open time, open, high, low, close, volume base, close time, volume quote, number of trades, taker buy volume base, taker buy volume quote, ignore.
- Timestamp **SPOT** de arquivo desde **2025-01-01 em microssegundos**; nos arquivos históricos anteriores, milissegundos conforme especificação do dataset. Validar a coerência entre unidade esperada, mês do arquivo e intervalo de timestamps. **Não detectar unidade só pelo número de dígitos.**
- Não usar `close_time` nativo para inferir causalidade: calcular `end_at` via abertura + intervalo de 1 minuto.
- Tratar `404` como `MISSING_NOT_CONFIRMED`, não como zero negociações. Tratar divergência/hash inválido como quarentena e erro visível.
- Revisão do ZIP publicado: novo hash gera novo `raw_object_id`, ligação `supersedes`, novo curated/snapshot; nunca sobrescrever histórico ligado a runs.
- `.part` antes de commit, retry com backoff finito, limite de arquivo e tamanho descomprimido, proteção contra ZIP Slip, validação de hosts após redirects, arquivo temporário com nome único, sem baixar tudo automaticamente.
- Política de dados spot 24/7: gaps são advertidos e bloqueiam janelas relevantes; zero volume **não** é por si só inválido.

### 6.4 Curated 1m, agregação e cobertura

Schema mínimo normalizado:

```text
instrument_id           STRING
open_at_utc             TIMESTAMP UTC
end_at_utc              TIMESTAMP UTC
available_at_utc        TIMESTAMP UTC (hipótese definida)
open, high, low, close   DECIMAL/FLOAT64 conforme uso, com bruto preservado
volume_base             NUMERIC >= 0
volume_quote            NUMERIC >= 0
trade_count             INT >= 0
source_object_id        STRING
source_revision         STRING
qa_status               PASS | FAIL | UNKNOWN
```

**Agregação:** somente janelas completas, início alinhado a UTC, `open=primeiro`, `high=max`, `low=min`, `close=último`, volumes somados. Janela 1h precisa conter 60 candles válidos; 4h, 240. Se incompleta, `coverage_ratio`, quantidade ausente e `reason` explícitos; **não fazer ffill de OHLC** e não negociar com essa barra. Warm-up inválido gera **não operar**, nunca preenchimento com futuro.

### 6.5 Manifests e estados de dados

**SQLite** começa com: `raw_objects`, `curated_partitions`, `dataset_snapshots`, `runs`, `run_events`, `experiment_trials`. IDs identificam conteúdo e versão. Entidades devem expor `created_at_utc`, `content_sha256`, `source_revision`, `status`. Uma tabela `snapshots_sources(snapshot_id, curated_partition_id)` fixa entradas. O banco registra caminho relativo e root configurável, não caminho absoluto do desenvolvedor. SQLite em WAL se houver mais de um leitor.

Estados de objeto: `DISCOVERED`, `DOWNLOADING`, `VERIFIED`, `NORMALIZED`, `QA_PASS`, `QUARANTINED`, `FAILED`, `SUPERSEDED`. A transição precisa ser persistida atomicamente com evidência. `UNKNOWN` de qualidade não se converte em `PASS` por conveniência.

### 6.6 Contrato de execução/reprodução

Cada run grava:

```text
artifacts/<run_id>/
  run.json                # ID, criada em, Git SHA se houver, lock hash, config hash
  strategy.json           # nome, versão, params, regra de warm-up
  sources.json            # snapshot, arquivos, versões, checksums
  execution_policy.json   # delay, fee, slippage, fill convention
  signals.parquet         # sinal desejado e timestamp
  fills.parquet           # execuções reais do simulador
  equity.parquet          # caixa, qty, valor marcado, equity por barra
  metrics.json            # líquidos, risco, turnover, exposição, benchmarks
  audit.json              # flags de dados, rejections, limitações, nível de evidência
  report.html             # abre sem servidor
```

`run.json` é **imutável depois de finalizado**. Repetir com mesmas entradas deve gerar métricas e fills equivalentes; o `run_id`/timestamp pode mudar. Hash lógico de conteúdo ignora metadados não determinísticos do container Parquet.

---
# PARTE III — MOTOR, CIÊNCIA E INTERFACE

## 7. Backtester canônico: especificação suficientemente exata para programar

### 7.1 Estados e fluxo

```text
SNAPSHOT APROVADO
  -> gerador de barras de decisão (1h/4h completas)
  -> FeatureView causal (apenas dados conhecidos em decision_at)
  -> Strategy.evaluate() -> TargetExposure[0..1]
  -> RiskGate -> APPROVE / VETO (+ reason)
  -> FillScheduler -> primeiro 1m open elegível
  -> FillModel(modelo de market order + custo declarado)
  -> Cash/PositionLedger (USDT e base asset)
  -> MarkToMarket -> métricas, audit trail e relatório
```

O MVP **não usa simulador vetorizado diferente do event-driven**: um único laço determinístico de execução sobre 1m e eventos de decisão, com otimizações quando houver necessidade real. Dados OHLCV não permitem modelar fila/partial fill; por isso M1 só simula **ordens a mercado preenchidas integralmente ou rejeitadas** e marca `fill_assumption=PROXY_OHLCV`. Sem ordens limit nessa fase.

### 7.2 Exposição e portfolio

- Moeda-base de avaliação: **USDT**. Um backtest de um instrumento possui `cash_usdt >= 0` e `base_qty >= 0`.
- Estratégias retornam target **0 ou 1** inicialmente: fora ou dentro do mercado; permitir exposições fracionárias apenas depois de contabilidade sólida.
- Compra: orçamento alvo limitado a caixa e taxa; se mercado ou mínimo de ordem inviabiliza, registrar **rejeição**, não fração impossível.
- Venda: quantidade base efetivamente disponível; tamanho ajustado a restrições configuradas. Tratar taxas na moeda do quote no MVP e sinalizar limitação; fee em base/terceira moeda só com modelo explícito posterior.
- Para lot/step/minNotional usar config auditada ou política simplificada identificada `SIMULATED_RULES`, nunca alegar backtest fiel ao histórico da venue quando apenas regras atuais foram usadas.
- Não permitir saldo negativo nem vender mais unidades do que possui. Sem borrowing/short/margem.
- No encerramento do recorte, **não forçar venda silenciosa**: `end_position_marked` faz avaliação da posição aberta; `close_on_end=true` é experimento alternativo com fee e fill declarados.
- `equity = cash_usdt + base_qty * mark_price`; não tratar lucro não realizado como caixa disponível.
- Buy & hold compra na primeira barra de execução elegível (respeitando warm-up da própria baseline; sem exigir warm-up de SMA) e mantém até o fim, sujeito à mesma regra de preço e custos do candidato.

### 7.3 Taxas e slippage sem contagem dupla

Configuração inicial ilustrativa, **não tabela de taxas atual da Binance**:

```toml
[execution]
mode = "spot_market_candle_proxy"
assumed_data_delay_seconds = 1
fill_on = "first_1m_open_strictly_after_available_at"
fee_bps_per_side = 10
slippage_bps_per_side = 5
fee_currency = "QUOTE"
allow_short = false
allow_leverage = false
```

Compra simulada: `execution_price = raw_1m_open * (1 + slippage_bps/10000)`. Venda: `raw_1m_open * (1 - slippage_bps/10000)`. Taxa quote debitada separadamente como `abs(qty*execution_price) * fee_bps/10000`. O modelo **não** adiciona spread por fora: o `slippage_bps` é uma **proxy agregada de custo de execução adicional** ao preço de candle, que pode representar spread/deslizamento; não alegar medição empírica. Uma rodada de compra+venda com 10 bps de fee e 5 de slippage por lado tem *aproximadamente* 30 bps all-in RT, sujeito à diferença de notionals e trajetória de preço.

Para análises de custos `0/5/10/20/40 bps round trip`, mapear cenários em **um campo `proxy_execution_cost_bps_rt`** aplicado metade em cada lado, **em substituição** à proxy anterior, preservando fee real/configurada **ou** usar o cenário como *custo total* e decompor sem dupla contagem. O relatório exibirá a convenção escolhida e os componentes.

### 7.4 Exemplo numérico de auditoria manual

**Fixture artificial independente de mercado:** saldo inicial = **1.000,00 USDT**, compra **1 unidade** a 100,00 USDT, vende a 110,00 USDT, taxa 0,10% em quote por operação, sem slippage. Resultado esperado:

| Evento | Caixa USDT | Quantidade | Taxa acumulada |
|---|---:|---:|---:|
| Inicial | 1.000,00 | 0 | 0 |
| Compra 1 @ 100, taxa 0,10 | 899,90 | 1 | 0,10 |
| Venda 1 @ 110, taxa 0,11 | **1.009,79** | 0 | **0,21** |

Lucro líquido **9,79 USDT**. Esta fixture verifica ledger e taxas. **Não** a confundir com um teste de sizing automático de 100% da carteira; o teste instrui quantidade fixa 1. Uma segunda fixture deve testar target=100% com arredondamento e fee, sem overdraft.

### 7.5 Invariantes mínimos de negócio

- Cash e quantidade nunca negativos; rejeições não modificam posição.
- A mesma intenção/evento de fill aplicada duas vezes não duplica dinheiro/quantidade.
- `sum(fee)` no ledger coincide com relatório; P&L fechado + não realizado reconcilia com equity (considerando injeções/retiradas, zero no MVP).
- Operação não tem fill anterior ao sinal e à disponibilidade.
- A estratégia não pode consultar `close`/`high`/`low` da barra em execução.
- Alterar as barras **futuras** não altera os sinais/ordens anteriores.
- `buy_and_hold` e estratégia são executadas sob **mesma política de custos e disponibilidade**.
- Sem dados suficientes para warm-up: sem sinal, sem inventar valores.

## 8. Estratégias do primeiro release

| Estratégia | Algoritmo | Parâmetros iniciais ilustrativos | Cuidado temporal |
|---|---|---|---|
| `cash` | Zero exposição | Nenhum | Caixa sem juros, explicitado. |
| `buy_hold` | Compra na primeira barra executável; mantém | Nenhum | Não compara curva sem custos. |
| `sma_cross` | 1 quando SMA rápida > SMA lenta, senão 0 | fast=20, slow=50 em 1h | Só usar closes **já encerrados**. |
| `breakout` | 1 se `close[t] > max(high[t-N..t-1])`; senão manter/zerar conforme regra versionada | N=20 | Canal exclui barra de decisão atual; o preço da ordem é futuro ao sinal. |
| `mean_reversion` (M2) | 1 quando z-score causal cai abaixo de limiar, sai acima de outro | lookback=30, buy_z=-2, exit_z=0 | Médias/desvios calculados até `t`, sem centralização. |

O M1 não inicia grid search. Deve permitir 1–3 configurações manuais. **Parâmetros mostrados são exemplos para exercitar software, não recomendações de trade.** Estratégias retornam `Signal`/`TargetExposure`, nunca editam saldo.

## 9. Métricas, comparações e relatório

### 9.1 Mínimo M1

- Capital inicial, capital final (mark-to-market), retorno líquido absoluto e percentual.
- P&L bruto, total de fees, proxy de execução/slippage, P&L líquido reconciliado.
- Máximo drawdown (com data de início/fim), curva de equity por frequência definida.
- Tempo exposto, quantidade de compras/vendas e operações fechadas; turnover.
- Retorno e drawdown do buy & hold no mesmo recorte/mesma política.
- Cobertura e janelas descartadas, convenção de execução, flags de qualidade.
- `EVIDENCE_LEVEL=DEMO|IN_SAMPLE` por padrão; não exibir “predição validada”.

### 9.2 M2

- Retornos por mês/semana, anualização explícita em mercado 24/7, Sharpe/Sortino quando a amostra permitir e taxas definidas, Calmar e duração de drawdown.
- Expectancy e payoff para round trips identificáveis, com contagem e observação de posições abertas; profit factor com caso de zero perdas tratado explicitamente.
- Exposição bruta/líquida, trade frequency e sensibilidade a custo, amostra/fold/ativo, concentração de P&L.
- Benchmarks de risco/exposição equivalente, sem confundir “ganhar menos B&H” com “falha” automaticamente.
- Para retorno diário e Sharpe, declarar a convenção (UTC, marcação diária, 365 dias/ano para cripto) e não inferir significância de um Sharpe alto sem análise de dependência temporal.

### 9.3 Saída visual obrigatória

**Página Dados:** resumo de origem, tamanho, data inicial/final, status PASS/FAIL/UNKNOWN, cobertura mensal, % gaps, `dataset_id`, `snapshot_id`, logs da ingestão, aviso quando é demo. Botão importar ZIP e botão baixar período **com confirmação explícita de ação**; nunca download de anos automaticamente.

**Página Backtest:** seletores instrumento/snapshot, timeframe de decisão, estratégia, parâmetros, capital, fee, slippage, executar (chama `run_backtest` do domínio). Gráficos de preço/sinais/equity/drawdown, benchmarks. Exibir `RUN ID` + botão abrir relatório.

**Página Operações:** tabela de decisões, vetos, ordens simuladas, fills, fees e P&L; filtros por data e export CSV. Clique numa operação revela hora do sinal, `available_at`, hora/preço de execução, estado do risco, preço bruto, taxa, slippage, motivo e fonte.

**Página Comparação (M2):** 2–4 runs sobre **mesmo intervalo, snapshot, política de preço** ou aviso vermelho textual de incompatibilidade; métricas e curvas sobrepostas. Não comparar resultados sem explicitar diferenças de configurações e períodos.

**Página Pesquisa (M2):** folds walk-forward, trials, fronteira treino/validação/holdout, robustez, estado do candidato `REJEITADO / INCONCLUSIVO / CANDIDATO_A_PAPER`. Status de holdout **SELADO** até solicitação formal de avaliação final.

**Página Paper Replay (M3):** relógio simulado, estados, risco, posições, eventos/rejeições e “PAUSAR/RETOMAR/RESETAR SIMULAÇÃO”; cada botão altera apenas simulação local, jamais exchange.

**Visual:** limpo, responsivo, datas UTC visíveis, unidades em USDT/%/bps corretas, estados claramente distinguidos por texto além de cor, empty states com instrução de próxima ação; sem gerar estatísticas fictícias para preencher telas.

## 10. Validação científica gradativa e proteção OOS

### 10.1 Categorias de evidência

| Tag | Significado | Permitido afirmar |
|---|---|---|
| `DEMO` | Dados sintéticos criados no projeto | “Motor e interface demonstrados.” |
| `IN_SAMPLE` | Um backtest exploratório histórico | “Resultado histórico exploratório, sujeito a sobreajuste.” |
| `WALK_FORWARD` | Treino e teste temporal rolante sob protocolo | “Resultado de validação temporal com incerteza.” |
| `SEALED_HOLDOUT` | Período final nunca consultado para ajustar hipótese | “Avaliação final pontual sob protocolo lacrado.” |
| `PAPER_REPLAY` | Replay histórico em sequência simulada | “Comportamento operacional reproduzido em histórico.” |
| `PAPER_FORWARD` | Coleta e decisões dali em diante | “Resultado observado ao vivo **simulado**, não execução real.” |

### 10.2 Períodos (proposta condicionada à cobertura)

**Apenas depois de qualificar a base**: BTC/ETH spot desde 2020 quando disponíveis; treino/desenvolvimento 2020-01 a 2023-12, seleção/validação 2024-01 a 2025-12, **holdout selado** 2026-01 a 2026-09. Nunca assumir cobertura por simples existência de URLs. Ao corrigir um período por ausência de dados, registrar a mudança **antes** de abrir estatísticas finais.

**Walk-forward M2:** janelas cronológicas com fold de treino e teste subsequente, sem shuffle, respeitando warm-up e fim da duração do label; avaliar intervalos com definição de preço e custos idêntica. Para estratégias **puramente determinísticas sem labels futuros no treino**, nem toda divisão requer purge. Para supervised learning com horizonte H, remover amostras cujos labels cruzem fronteiras e aplicar gap suficiente.

### 10.3 Regime de experimentação para evitar fabricação involuntária de Sharpe

1. `experiment.yaml`: hipótese, versão, instrumento, timeframe, métricas primárias, parâmetros permitidos, orçamento de tentativas, períodos e data de selamento.
2. Trial alterado após observar resultado conta como novo trial (inclusive seed, thresholds, janela, ativo, feature e custo).
3. Realizar avaliação sob **custo-base** e estresse 2×/3×, com premissas claramente indicadas; não exigir que todo investimento de baixa frequência sobreviva a custo absurdo sem racionalidade.
4. Comparar com cash e buy & hold; contextualizar drawdown/exposição e usar risk-matched mais tarde.
5. **Só acessar holdout final quando uma família candidata estiver congelada**. Resultado ruim não pode ser “consertado” mudando parâmetro e testando o mesmo holdout outra vez.
6. `CANDIDATO_A_PAPER` significa passou critérios pré-registrados de pesquisa; não significa edge garantido nem sugestão de negociar capital real.
7. Métodos DSR/PBO/Reality Check são **backlog avançado**, usados apenas quando número/desenho dos trials e tamanho de amostra justificarem seus pressupostos. Não codificar estatística simbólica só para inserir siglas em PDFs.

### 10.4 Protocolo ML M3 (modesto, mas correto)

- Alvo inicialmente retorno futuro contínuo em `H` barras **ou** classificação de retorno acima de custo fixado; escolher um por experiência e explicitar instante em que `label` se revela.
- Features pequenas: retornos defasados, range relativo, vol histórica, volume relativo, distância de média/máximo; nulos apenas por warm-up.
- Modelo nulo → Ridge/LogisticRegression → `HistGradientBoosting` do scikit-learn **ou** LightGBM (escolher 1, não três bibliotecas de boosting).
- Transformações `fit` somente treino de cada fold; ajuste de threshold apenas validação interna; out-of-fold predictions com carimbo temporal.
- No máximo **10 trials de hiperparâmetros** por família na primeira prova de ML; evitar inventar otimização infinita.
- Métricas preditivas (MAE/Brier/log loss conforme tarefa) **e** efeito financeiro com mesmo executor/custos. Calibração somente quando modelo realmente produzir probabilidades.
- ML que não supera controle simples no mesmo OOS deve ser marcado `SEM_GANHO_INCREMENTAL`, sem fallback mágico a rede neural.

## 11. Risco e papel do paper trading

**M1/M2:** sem processo vivo, sem conectores de ordens. O backtest aplica veto para dados inválidos, saldo insuficiente, minNotional/tamanho sob política definida, gap na barra elegível e limites de exposição. Limites exibidos na UI.

**M3 paper replay:** lê barras em ordem temporal e emite eventos como se chegassem, sem antecipar acesso ao arquivo inteiro pelo código da estratégia; permite pause/restart; checkpoint em SQLite ou arquivo atômico; eventos com idempotency key; ledger reaplicável. `HALTED` após dados inválidos, exceção crítica, divergência de caixa/posição ou violação de risco.

**M4 paper forward:** só com observações realmente recebidas após a ativação, fonte incremental e métricas de frescor. Verificar se há stream/REST adequado e termos antes de escolher o conector. Candle em formação fica **não elegível**. Fonte offline ou diariamente publicada **não** é chamada de “tempo real”. Sem ordens reais e sem usar senhas de exchange.

---

# PARTE IV — ROTEIRO DE CONSTRUÇÃO POR MARCOS

## 12. Regra de execução para o Codex

Cada fase entrega **um caminho percorrível pelo usuário**, código real e documentação do que foi feito. O Codex não deve somente preencher pastas ou expandir planos. Cada fase tem:

1. Entrada conhecida, arquivos esperados e um comando demonstrável.
2. Implementação mínima integrada às fases anteriores.
3. **Verificação proporcional ao risco:** no máximo 1 execução de testes focados **e** 1 smoke/manual scripted por fase, exceto ao corrigir defeito concreto; nenhuma suíte infinita nem autotuning.
4. Um `docs/CHANGELOG_IMPL.md` com feito, não feito, comandos reais e limitações.
5. Um roteiro manual de 3–8 passos que **o dono do projeto possa repetir**.
6. Handoff: o próximo prompt parte do repositório presente, não recomeça projeto do zero.

**Exceção séria à redução de testes:** temporalidade, reconciliação de dinheiro, ZIP/checksum e ausência de negociação real merecem verificações focalizadas; não eliminar esses testes para economizar alguns minutos.

## 13. Sequência completa, dependências e aceites

### F0 — Fundação e demo offline | porta de entrada

**Objetivo:** projeto executável sem rede e sem dados financeiros reais.

- Criar ambiente, README, CLI, config, `AGENTS.md`, instruções Windows PowerShell e WSL/Bash.
- Fixture sintética explícita com 10–30 minutos **para ledger** e conjunto maior de candles sintetizados para gráfico/SMA (sem “dados reais” falsos).
- Estruturar domain, writer de runs, leitura dos artefatos e relatório HTML básico.
- Um backtest mínimo com `cash` e `buy_hold` sobre fixture, com funções reais e um único laço de execução.
- Versão simples de painel ou página HTML com rótulo **DADOS SINTÉTICOS**; sem fazer dashboard todo antes do motor.
- Salvar `docs/DECISIONS.md`, `docs/MANUAL_QA.md` e `AGENTS.md`.

**Aceite manual:** `uv sync`, `uv run quantlab demo`, `uv run streamlit run src/quantlab/ui/app.py` (ou comandos equivalentes documentados) criam/abrem artefatos e mostram explicitamente a fonte sintética. Uma compra/venda manual reproduz a conta de 1.009,79 USDT descrita na §7.4.

### F1 — Aquisição oficial controlada

**Depende:** F0. **Entrega:** um mês spot BTCUSDT descarregado com garantia de integridade.

- CLI `data plan --symbol BTCUSDT --start 2024-01 --end 2024-01`: mostra URLs, n de ZIPs e espaço estimado quando conhecido; modo `--offline` identifica arquivos já disponíveis.
- CLI `data fetch`: baixa ZIP + CHECKSUM oficial, valida sha, salva raw imutável e manifest.
- CLI `data import --zip PATH --checksum PATH`: mesma lógica de validação para usuário offline, com exceções transparentes se checksum não for fornecido (integridade da fonte `UNVERIFIED`, não `PASS`).
- Estados de erro: offline, 404, rate-limit, checksum inválido, diretório sem permissão, disco insuficiente, host fora de allowlist.
- *Idempotência:* rodar duas vezes sem duplicar; revisão remota cria novo ID.

**Aceite manual:** baixar 2024-01 BTCUSDT 1m, inspecionar ZIP/CHECKSUM, executar novamente e ver `SKIPPED_ALREADY_VERIFIED`; adulterar uma cópia no import e observar erro/quarentena, sem sobrescrever versão válida. Se não houver internet, importar ZIP amostra conhecido + checksum (sem alegar que o download remoto foi verificado).

### F2 — Normalização, qualidade e agregação

**Depende:** F1. **Entrega:** um snapshot realmente consultável e auditado.

- Parser com 12 campos, datas e precisão corretas; suporte a spot 2024 ms e 2025 us.
- QA: OHLC coerente, preços positivos, volume não negativo, trade count, tipos, duplicatas, intervalos ordenados, gaps e cobertura.
- Curated Parquet mensal; no máximo um conjunto controlado de arquivos por instrumento/mês, sem tiny-files por candle.
- Agregação 1m→1h/4h de janelas completas; rejeição/flag de incompletos.
- Catálogo e snapshot imutável; relatórios de qualidade em JSON/HTML.

**Aceite manual:** carregar 2024-01 BTC 1m, consultar primeira/última vela, contar minutos observados e gaps; verificar os 60 candles de uma hora contra OHLC agregado; importar fixture 2025 de microssegundos e conferir ano correto, não 57 mil anos no futuro.

### F3 — Simulação financeiramente consistente

**Depende:** F2. **Entrega:** **primeiro backtest real e auditável**.

- Adaptar engine F0 para 1m reais com decisões em 1h/4h.
- cash, buyhold, SMA, breakout; ordens spot a mercado com timing, cash, qty, taxas, custo proxy, veto e equity.
- Dados faltantes: interromper decisão/fill em região contaminada; não completar usando ffill ou preço futuro.
- Persistir signals/fills/equity/metrics e run manifest.
- Exibir buy & hold sobre mesmo período e política.
- Custo e atraso configuráveis; relatório distingue lucro bruto, custos, lucro líquido e posição aberta.

**Aceite manual:** executar mesmo run duas vezes e comparar métricas e fills; simular SMA com taxas 0 e com taxas positivas e ver reconciliação; inspecionar 3 trades com timestamps `signal < fill`; conferir cash final e `equity=cash+base_qty*mark`. Não otimizar modelos nem baixar histórico gigante nesta fase.

### F4 — Interface utilizável e pacote M1

**Depende:** F3. **Entrega:** aplicação local para usar sem editar código.

- Streamlit **Dados / Backtests / Operações**, seleção de snapshot, estratégia, janela, custos e botão executar.
- Abrir runs anteriores, baixar HTML/CSV, navegar de run até dataset e política de execução.
- Erros com instruções para o usuário; app nunca lê credenciais privadas nem executa código arbitrário das configs.
- README atualizado com tutorial ilustrado em texto, comandos Windows e WSL, troubleshooting básico.
- Empty state/fixture sintética, distinção clara entre demo e histórico real.

**Aceite manual M1:** começar sem dados, executar demo, escolher importar/baixar um mês, ver QA, rodar SMA e buyhold, examinar trades, baixar relatório e reiniciar app recuperando os runs. **M1 pode ser declarado entregue aqui**, mesmo que nenhuma estratégia tenha resultado positivo.

### F5 — Pesquisa séria, múltiplos meses e comparação | pacote M2

**Depende:** F4. **Entrega:** comparar hipóteses sem confundir tuning e ciência.

- Downloader multi-mês incremental limitado, BTC/ETH, sincronia diária/mensal quando necessário.
- Backfill histórico planejado **sob comando e armazenamento disponível**; resgatar revisões, não misturar arquivos mensais/diários sobrepostos silenciosamente.
- Comparador de runs com incompatibilidade sinalizada; análise de regime/período e sensibilidade de custo.
- Registro de famílias/trials e benchmark risk/exposure-matched quando viável.
- Walk-forward de regras temporais (com splits e tempo de execução explícitos), holdout selado e abertura formal apenas uma vez por família congelada.
- Relatório com diagnóstico de concentração de ganhos, tempo no mercado, drawdown e decisão `INCONCLUSIVO` possível.

**Aceite manual:** rodar 2 estratégias no mesmo snapshot e explicar diferenças de custo; mostrar o selo de holdout sem ler resultados; liberar avaliação final de uma versão congelada e registrar a irreversibilidade; reproduzir um fold e um trial com hashes iguais.

### F6 — Machine learning sem truques | primeira fase M3

**Depende:** F5. **Entrega:** um preditor comparado honestamente com baseline.

- Um label e H configuráveis, causalidade e observação final do alvo claras.
- Features mínimas; `Pipeline` fit somente treino, folds temporais e purge quando houver labels sobrepostos.
- Modelo nulo + LogisticRegression/Ridge (conforme alvo); segundo modelo opcional somente se necessário.
- Até 10 tentativas de tuning por família inicialmente, todas registradas; uma métrica preditiva apropriada e uma métrica econômica.
- Previsão -> target/risk -> **mesmo motor de F3**, mesma marcação, custos e universo.
- Gráficos de calibração apenas em classificação probabilística, e ablação `preço` vs `preço+volume`.

**Aceite manual:** escolher fold, consultar intervalo de treino, primeira previsão e data da label; alterar artificialmente dados futuros e verificar que previsões históricas não mudam; comparar modelo nulo/linear/estratégia simples, inclusive resultado “ML perdeu”.

### F7 — Paper replay resiliente | segunda fase M3

**Depende:** F6 ou F5 (ML não é obrigatório). **Entrega:** comportamento sequencial e operacionalmente auditável.

- Processo **offline** avança o relógio por candles fechados, sem acesso futuro nas views da estratégia.
- Máquina `STOPPED→READY→RUNNING→DEGRADED/HALTED` e causas explícitas.
- RiskGate com exposição máxima, veto de dado stale/gap, limite de drawdown da simulação, fail-closed.
- Persistência/checkpoints e replay idempotente; UI com pausa/reinício e evento de execução.
- Comparar replay com backtest da mesma janela e explicar diferenças.

**Aceite manual:** pausar no candle selecionado, reiniciar processo, garantir que fills não duplicam, introduzir gap e verificar que estado vira `HALTED` sem novos fills; reset explícito volta ao início em nova execução separada.

### F8 — Paper forward, perps, L1/L2, novas fontes | M4+ sob decisão

**Depende:** F7 e hipóteses próprias. Abrir uma RFC por recurso, não um épico infinito. Candidatos em ordem condicional: (a) paper forward alimentado por mercado real, (b) venue secundária para confirmação, (c) perp com funding/mark/contrato, (d) L1 e custo de spread observado, (e) macro/ALFRED com vintages, (f) on-chain/notícias com timestamp e licença, (g) L2/L3 e integração Nautilus, (h) análises estatísticas avançadas. **Nenhum item deve ser marcado “pronto” antes de ter entrega isolada verificável**.

## 14. Matriz de dependência e prioridade de implementação

| Fase | Necessária para M1? | Pode usar subagentes? | Tipo de validação | Bloqueio duro |
|---|---|---|---|---|
| F0 Demo | Sim | 1 auxiliar de docs/UX se não mexer no domínio | Fixture numérica + smoke | Ledger incorreto |
| F1 Ingestão | Sim | 1 auxiliar revisando documentação Binance | Checksum + rerun manual | RAW adulterado |
| F2 Dataset | Sim | Auditor temporal read-only | ms/us + hora agregada + gap | Timestamp ou QA inválidos |
| F3 Backtest | Sim | Revisor de contabilidade/timing independente | Fixture ledger + 3 trades manuais | Lookahead/fees incorretos |
| F4 UI | Sim | UI em arquivos isolados, engine congelada | Navegação end-to-end | Tela que finge dados |
| F5 Ciência | Não, M2 | Estatística com contrato de entrada estável | Split/holdout manual + smoke | Contaminação OOS |
| F6 ML | Não, M3 | Revisor de leakage | Fold e previsão causal | Vazamento de label |
| F7 Replay | Não, M3 | Revisor de estados e ledger | Restart + halt manual | Ordem duplicada |
| F8 Expansão | Não, sob RFC | Somente subprojetos isolados | Aceite por feed/modelo | Falta de dados/licença |

**Paralelismo prudente:** um agente implementa o **núcleo**, no máximo um agente auxiliar produz documentação, pesquisa de API ou revisão em arquivos diferentes. Não há ganho em três agentes reescrevendo `ledger.py` ao mesmo tempo.

---

# PARTE V — OPERAÇÃO, VERIFICAÇÃO E CRITÉRIOS DE PRONTO

## 15. Contrato de eficiência: testes suficientes, não compulsivos

**Por fase:**

- Executar **uma verificação automatizada direcionada** que cubra o novo risco crítico (quando houver), e **um smoke end-to-end** que produza arquivo/saída visível.
- Rodar lint **somente no código alterado** ou no pacote pequeno, uma vez ao fechar a fase.
- Evitar `pytest` completo em loop, mutation/fuzz/load/property testing em massa, CI extensa, múltiplos builders e benchmarks globais sem bug identificado.
- Em caso de falha, diagnosticar, corrigir e reexecutar **somente o teste falho**; uma vez concluído, avançar.
- Testes precisam ser **determinísticos, offline, curtos**. Integração real HTTP não pode ser requisito de CI. Downloads grandes são checagens manuais explícitas, não fixtures do repositório.
- **Nunca** marcar uma fase como aceita quando a contabilidade/timing/integridade estão sabidamente errados. Se prazo/conteúdo estourar, reduzir escopo não crítico, não eliminar controles financeiros.
- Se a funcionalidade ainda não tiver dados suficientes, mostrar `NOT_IMPLEMENTED` ou `UNKNOWN` honestamente em vez de inventar resultado.

## 16. Checklist manual consolidado do proprietário

**Instalação e demo**

- [ ] Instalou dependências travadas a partir de repo limpo.
- [ ] `demo` gerou run e relatório; tela marcou dados sintéticos.
- [ ] Fixture fixa de compra/venda fechou em 1.009,79 USDT com taxas 0,10%.
- [ ] Desligou a internet e conseguiu abrir demo/runs locais.

**Dados históricos**

- [ ] Planejou mês antes de baixar e viu nome/URL de arquivo.
- [ ] ZIP e CHECKSUM conferidos; segunda execução não duplica.
- [ ] Origem 2024 gerou datas 2024, origem 2025 gerou datas 2025.
- [ ] Um mês tem cobertura explícita; gaps/duplicatas aparecem como flags.
- [ ] Uma agregação de 1h corresponde aos 60 minutos constituintes.

**Backtesting**

- [ ] Rodou cash, buyhold e SMA sobre o **mesmo dataset/intervalo**.
- [ ] Sinal de 1h às 15h não teve fill no open das 15h com delay positivo.
- [ ] Mudar fee/custo mostrou diferença explícita na contabilidade.
- [ ] UI mostrou `execution_policy`, run ID, snapshot ID e origem dos dados.
- [ ] Verificou pelo menos 3 operações, incluindo compra e venda, com saldos e preço.
- [ ] Reabriu a aplicação e achou o run já salvo.
- [ ] Exportou relatório HTML e CSV de fills, que abrem sem a UI.

**Pesquisa M2/ML**

- [ ] Viu fronteiras de treino/validação, resultado final lacrado antes de aprovação.
- [ ] Comparou mesmas regras com custo-base e custo-estresse.
- [ ] Conseguiu registrar uma estratégia rejeitada e uma inconclusiva.
- [ ] Modelo ML não lê features/folds futuros e recebe tag correta de evidência.

**Paper replay**

- [ ] Pausa/retomada não muda resultado no mesmo seed/dados.
- [ ] Reinício não duplica trade nem cobra fee duas vezes.
- [ ] Falha de dados impede novas execuções; `HALTED` visível.
- [ ] Em lugar nenhum da UI há botão “comprar na Binance”.

## 17. Comandos esperados (o Codex os implementará)

> **Não estão implementados neste documento.** A fase F0 deve fixar sintaxe definitiva no README. Exemplos de contrato:

```bash
uv sync
uv run quantlab --help
uv run quantlab demo
uv run quantlab data plan --symbol BTCUSDT --start 2024-01 --end 2024-01
uv run quantlab data fetch --symbol BTCUSDT --start 2024-01 --end 2024-01
uv run quantlab data audit --symbol BTCUSDT
uv run quantlab data snapshot --symbol BTCUSDT --start 2024-01 --end 2024-01
uv run quantlab backtest --snapshot <SNAPSHOT_ID> --strategy sma_cross --timeframe 1h
uv run quantlab runs show <RUN_ID>
uv run streamlit run src/quantlab/ui/app.py
```

No Windows PowerShell, comandos iguais com `uv` no PATH. No WSL, configurar paths Linux e **não** escrever catálogo no `/mnt/c` em processamento de milhões de linhas se isso estiver causando gargalo.

## 18. Política de corte: o que fazer quando uma etapa não cabe

**Manter sempre:** dados da fonte corretos, causalidade temporal, ledger reconciliável, arquivo de run, CLI funcional, informação real de falha. **Cortar primeiro:** dashboards avançados, variedade de indicadores, exportações extras, personalização visual, benchmark segundo engine, múltiplos provedores, complexidade estatística, ML adicional, paper forward. O sistema tem permissão para dizer **“não sei”** e **“não demonstrado”**.

## 19. Segurança, manutenção e ambiente

- Sem endpoint de broker privado; dados históricos apenas em hosts reconhecidos. Configs não podem carregar módulos arbitrários, rodar subprocessos ou abrir URLs não validadas.
- Nunca logar secrets; `.gitignore` cobre `.env`, `data/raw`, `state`, artefatos grandes e `.venv`, preservando pequena fixture sintética.
- Backup de SQLite/manifests/config e de datasets não recuperáveis; identificar artefatos recuperáveis de fonte pública.
- Relatórios de erro em português: causa, objeto afetado, etapa e instrução concreta de reparo; nunca `except: pass`.
- Registrar `requirements/python`, lock SHA, código Git SHA quando repositório tiver commit. Não exigir Git SHA válido para uma primeira execução local sem commit; registrar `UNCOMMITTED` com advertência.
- **Sem obrigação de rodar 24/7** até a fase paper forward. Sem publicidade, SaaS, conta de cliente ou capital real.
- Respeitar contratos de uso dos datasets; licença de código no GitHub **não** é automaticamente licença irrestrita para comercializar os dados.

## 20. Definições inequívocas de pronto

**M1 ACEITO** se uma pessoa, seguindo apenas `README.md`, consegue importar 1 mês oficial, auditar, executar SMA + buy & hold com custos e ver/exportar operações e relatório, e os checks críticos de timestamp/ledger/ZIP passam. Caso a rede bloqueie a Binance, **M1 LOCAL** pode ser demonstrado por import validado, mas o download remoto fica claramente `NÃO VERIFICADO`.

**M2 ACEITO** se runs comparáveis, vários meses, walk-forward e holdout protegido estão implementados com provas documentadas; não exige estratégia lucrativa.

**M3 ACEITO** se um modelo simples tem inferência causal e comparação honesta e/ou paper replay segura, retomável e auditável; a ausência de ganho preditivo não impede aceitar engenharia correta.

**M4 ACEITO** **por recurso** e só após nova definição: paper forward não é replay; perp não é spot alavancado; L2 não é L3; macro revisado não é dado conhecido no momento original.

---

# PARTE VI — COORDENAÇÃO DE AGENTES CODEX E REFERÊNCIAS

## 21. Política para GPT-6 Luna, High / Extra High

- **Preferência:** agente principal Codex com **GPT-6 Luna em High**, caso disponível na interface; **Extra High (`xhigh`)** nas fases F2/F3 (tempo/contabilidade), F5/F6 (leakage) e nas revisões independentes de bugs críticos.
- Configurar o esforço **no seletor ou configuração real da ferramenta**. Instruções em texto **não garantem** mudança de modelo/effort. Não inventar comando ou slug de Codex que o ambiente não aceita.
- Subagentes: chamar somente se o ambiente Codex oferecer orquestração; o auxiliar pode checar documentação da Binance, contratos de risco ou revisar um commit, **sem escrever arquivos de domínio simultaneamente**.
- Orquestrador trabalha uma fase por vez, faz handoff descritivo e para. **Não** dar um prompt monstruoso pedindo F0–F8 de uma vez; uso de contexto e conflitos cresce sem melhorar entrega.
- Não rodar ciclos automáticos de 100 análises, testes ou benchmarks. Uma verificação focalizada + um smoke por fase, refinada diante de defeitos reais.

## 22. Como usar os dois Markdown no Codex

1. Criar repositório `quantlab/` vazio.
2. Copiar **este plano** e o arquivo `PROMPTS_CODEX_QUANTLAB_GPT6_LUNA.md` para `docs/` (ou fornecer arquivos ao Codex com caminhos claros).
3. Ajustar o modelo/esforço pela interface quando possível.
4. Enviar **Prompt 00 (contrato do projeto)** + **Prompt F0** do segundo arquivo na primeira sessão, e só então F1, F2, etc.
5. Ao final de cada etapa, inspecionar os comandos de execução e **efetuar os passos manuais**. Se há bug, usar prompt de correção focalizada, não mandar reescrever o projeto.
6. Não permitir que um agente substitua silenciosamente decisões travadas; mudanças justificadas vão para `docs/DECISIONS.md`.

## 23. Referências técnicas para implementação, não requisitos de dependência

- Arquivos Binance, schema/timestamps, checksum e revisões: <https://github.com/binance/binance-public-data>
- DuckDB Parquet: <https://duckdb.org/docs/current/data/parquet/overview>
- DuckDB ASOF join: <https://duckdb.org/docs/current/sql/query_syntax/from>
- uv / lockfile: <https://docs.astral.sh/uv/guides/projects/>
- Freqtrade e lookahead: <https://docs.freqtrade.io/en/stable/lookahead-analysis/>
- vectorbt, restrições de licença: <https://vectorbt.dev/terms/>
- NautilusTrader: <https://nautilustrader.io/docs/latest/>
- scikit-learn / time series split e pipelines: <https://scikit-learn.org/stable/modules/cross_validation.html>
- Streamlit: <https://docs.streamlit.io/>
- Configuração de esforço dos modelos: <https://developers.openai.com/api/docs/guides/reasoning>
- Orquestração multiagente depende da plataforma: <https://developers.openai.com/api/docs/guides/agents-api/multi-agent>

**Última regra:** este laboratório serve para **descobrir se existe ou não vantagem**, não para declarar uma vencedora antes dos dados. Um backtest que rejeita uma hipótese com integridade é uma entrega melhor que um gráfico espetacular gerado com vazamento.

---

# ANEXO DE EXPANSÃO — COMO TORNAR O BACKTESTING EXTREMAMENTE ROBUSTO SEM SABOTAR A PRIMEIRA ENTREGA

> Este anexo **não aumenta o escopo de F0–F4**. É o mapa técnico para investir na profundidade de dados e na ciência **após** existir um software utilizável. Cada expansão exige uma hipótese e um aceite manual novos.

## A. Matriz de aquisição por incremento de valor

| Ordem | Fonte/mercado | Dados desejados | Por que acrescentar | Barreiras de correção | Gate antes de construir |
|---:|---|---|---|---|---|
| A0 | Binance spot | BTC/ETH klines 1m e metadados | Base repetível, candles e custos hipotéticos | ms/us spot, revisões de ZIP, gaps | F1–F4 concluídas |
| A1 | Binance spot | Trades/aggTrades históricos | Analisar fluxo, construir vela/trade e comparar com klines | IDs de trade, agressor, quantidade, footprint; sem confundir aggTrade com trade individual | Hipótese de fluxo que candles não respondem |
| A2 | Binance USD-M perp | Bars, trades, mark/index, funding e contratos | Testar carry, funding e tendência de perp | Distinção forecast/realizado, taxas, liquidação, alavancagem e precisão própria do feed | Ledger de derivativos independente |
| A3 | OKX spot/perp | Histórico comparável | Robustez entre venues, limitação de seleção por exchange | Regras contratuais e temporalidade próprias; mapeamento de instrumentos | Uma estratégia já avaliada em Binance |
| A4 | Fonte BBO/L1 | Best bid/ask e tamanhos | Substituir parte da proxy de custo por observações reais | Cobertura temporal, atraso e disponibilidade do feed | Estratégia sensível a spread demonstrada |
| A5 | Book L2 incremental | Snapshots + deltas + seq/CRC | Modelar profundidade/liquidez e parte de slippage | Gap invalida livro até ressync; não conhece fila individual | Necessidade econômica de fills intrabar |
| A6 | L3/MBO (vendor) | Ordens individuais e sequência | Pesquisa de fila/market making específica | Custos/licença e cenário contrafactual de nossa ordem | Estratégia de alta frequência justificada |
| B1 | FRED/ALFRED | Releases, séries econômicas e vintages | Estudar eventos macro com revisão temporal | `observation_date` ≠ `release_at`; revisões restroativas | Hipótese causal de macro |
| B2 | BCB SGS/CVM/B3 | Macro e ações Brasil | Expandir universo e possíveis fatores fundamentalistas | Calendário, corp actions, delistados, frequência e direitos B3 | Abrir ramo `equities_br` separado |
| B3 | SEC/EDGAR + provider de equity | Filings e preços EUA | Expandir cross-sectional/survivorship-free | Publicação, ajustes de preço, delisting e universo histórico | Abrir ramo `equities_us` separado |
| C1 | On-chain | Série derivada e/ou dados de blockchain | Fluxos e regime on-chain | Rótulos recalculados, mudança de metodologia e disponibilidade passada | Ablação `preço` vs `preço+onchain` |
| C2 | Notícias | Headlines/artigos versionados | Estudos de eventos sem “notícia do futuro” | Primeiro texto disponível, edições posteriores, licenças | Metodologia PIT de texto aprovada |
| C3 | Sentimento/LLM | Tags de notícias e entidades | Feature textual com custo mensurado | Prompt/model version, extrapolação retroativa, time lag | Valor incremental medido em C2 |
| C4 | Derivativos de opções | Quotes, trades, greeks e curvas de volatilidade | Pesquisa de volatilidade implícita | Corporate/contract specs, maturidade, exercício, spreads | Hipótese e feed auditados |

**Fontes candidatas que não devem ser presumidas grátis:** Tardis, Kaiko, CoinAPI, Databento, Massive/Polygon, Amberdata, Glassnode/CryptoQuant e similares. Confirmar licença/limites **na data efetiva de aquisição**. Usar `CCXT` apenas como adaptador quando couber; ele não inventa retrospectivamente profundidade histórica ausente da exchange. A planilha extensa de fornecedores e preços do anexo original permanece **pesquisa de referência**, não orçamento vinculante desta v2.

## B. Contrato universal para incorporar uma nova fonte

Antes de uma linha de ingestão, escrever `docs/rfc-data/RFC-<ID>.md` com:

1. Hipótese econômica a ser testada; por que dados atuais não bastam.
2. URL da documentação oficial, schemas de exemplo, moeda/unidade, origem, classe de ativo e tipo de mercado.
3. Cobertura histórica **real por símbolo**, instrumento deslistado, timezone, periodicidade, limites e custo.
4. `event_at`, `published_at`, `received_at`, `available_at`, revisão/vintage, hipótese de atrasos.
5. Endpoint batch vs stream, limite de requisição, checksum e licença para armazenar/redistribuir.
6. Como um dia de dados será rebaixado, comparado e reprocessado determinísticamente.
7. Chave de deduplicação e política para evento corrigido.
8. Cálculo de custo total anual em BRL incluindo taxas/frete não pertinente, armazenamento/cloud/imposto e tempo de manutenção.
9. Ablação: como medir se acrescenta algo sobre preço/volume.
10. Condição objetiva de NÃO ADOTAR a fonte.

**Protocolo de compra:** amostra pequena → normalizador → QA → snapshot → uma única hipótese pré-registrada → ablação → decisão. Nunca assinar “todos os dados possíveis” antes de formular perguntas. Os dados de baixa qualidade também aumentam a capacidade de fabricar falsa significância.

## C. Arquitetura de dados avançada sem migrar prematuramente

### Três camadas de acesso

- `raw` nunca alterado, formato original, manifesto/captura e revisão.
- `curated` schema unificado **por tipo de mercado**: spot, perp, options, equities possuem ledger/calendário/contratos diferentes; não forçar um megatable com mil `NULL`.
- `research_snapshot` conjunto imutável de IDs + PIT cutoff + features versionadas e relatório de qualidade; referência exclusiva para treino/backtest reprodutível.

Armazenar grandes datasets particionados por `provider/venue/market_type/instrument/dataset/year/month`, ajustando frequência quando ticks exigirem partições diárias. Usar Parquet columnar, partições balanceadas e DuckDB/Polars antes de pensar em ClickHouse ou cluster. SQLite registra catálogo; resultados e métricas são pequenos. Partições de Parquet carregam `data_version`, `parser_version`, `logical_hash`, `source_object_ids`, `quality_state`, `min/max_timestamp`.

### Tabela mínima de eventos temporais futuros

```text
record_id                identidade nativa + revisão
instrument_id            exchange + mercado + contrato
observed_at              fenômeno observado
published_at             publicação documentada (nullable)
received_at              coletor recebeu (horário real)
available_at             primeiro instante justificado/assumido
available_at_confidence  OBSERVED / DOCUMENTED / ASSUMED / UNKNOWN
valid_from/to            janela de validade ou vigência de versão
source_revision          versão de observação
raw_object_id            hash/proveniência
```

Para macro/edits de notícias: `vintage_at`, `release_id`, `supersedes`, texto original, hash e horário original de publicação. Um `ASOF` JOIN simples garante **ordenação**, mas não garante sozinho que a observação casou com sua **versão historicamente correta**. Se essa evidência estiver `UNKNOWN`, não habilitar uso em conclusão causal.

## D. Evolução da fidelidade de backtest (níveis e limites)

| Nível | Entradas | O que consegue estimar | O que NÃO pode afirmar |
|---|---|---|---|
| E0 | 1m OHLCV | Estratégias de 1h/4h, cash/position/fees e proxy de slippage | Preço real de execução, fila, impacto e trajetória intraminuto |
| E1 | Trades tick + calendário | Confrontar last trade e dinâmica mais fina | Melhor bid/ask verdadeiro sem quote, fila |
| E2 | Bid/ask L1 | Spread histórico observado, execução agressora proxy | Profundidade completa, fila, capacidade de grande ordem |
| E3 | L2 + snapshots | Liquidez por preço e atualizações de book | Ordem individual L3 e efeito contrafactual da nossa participação |
| E4 | MBO/L3 | Aproximação de fila e prioridade individual | Fill de nossa ordem com perfeita fidelidade ao mundo sem ela |

### Como testar a melhoria de fidelidade

Escolher **o mesmo trade list** e comparar E0 contra E1/E2 em um intervalo pequeno com preços de execução suficientemente próximos e documentação de diferenças. A partir de E2, fazer estudo de **erro da proxy de custos**, não recalibrar sinal no holdout. Não trocar engine porque novos resultados são mais lucrativos; primeiro explicar por que são mais realistas.

### Quando experimentar NautilusTrader/Freqtrade/vectorbt

- Freqtrade: controle externo para entender candles/lookahead e alternativas de dry-run; não canonizar seus fills junto dos nossos sem mapear regras.
- NautilusTrader: candidato para evento/tick/book e possível arquitetura operacional, **somente** com fixture de equivalência e contrato por market type.
- vectorbt: opcional para triagem maciça e grids após existir registro rigoroso de trials; verificar Commons Clause para distribuição/comercialização.
- LEAN/Qlib: referência de arquitetura, nunca dependência por prestígio.

## E. Pipeline de pesquisa “parrudo” em níveis observáveis

### Nível S0 — correção operacional (M1)

Fixture contábil fechada; timestamp sem futuro; QA/integridade; cash/buyhold; custo explícito. Produz `DEMO` ou `IN_SAMPLE` e não tenta inferir edge.

### Nível S1 — robustez econômica (M2)

- Dados 2020–2026-09 quando realmente disponíveis, múltiplos ativos, mesma política de execução.
- Walk-forward temporal e janela final selada; custo nominal e estresse 2×/3×; breakdown por mês/trimestre/ano, bull/bear conforme critério **causal** e congelado.
- Contagem dos trials; superfície de parâmetros pequena e interpretável; quantidade de trades, custos, drawdown e parcela do lucro concentrada nos 1/5/10 maiores trades.
- Controle negativo: sinais defasados/aleatórios com pareamento aproximado de turnover/exposição (reportar limitação da comparação).
- Transparência sobre amostra efetiva e incerteza. Uma vela por minuto não equivale a uma observação estatística independente.

### Nível S2 — inferência e seleção (M2+, depois do S1)

- Bootstrap em blocos de retornos ou trades quando justificado por dependência temporal, com intervalo de confiança.
- Testes de sensibilidade a atraso de execução, custo e períodos excluídos; não exigir desempenho positivo em **todo** subperíodo como certificado absoluto.
- Ajuste para seleção múltipla: DSR quando Sharpe e número efetivo de tentativas permitirem interpretação; PBO/CSCV quando houver matriz coerente de estratégias em blocos; Reality Check/SPA conforme desenho e custo de implementação.
- Registrar premissas e casos em que cada teste **não é aplicável**. Não usar p-value de 0,05 extraído de trades sobrepostos como licença de lucro.

### Nível S3 — OOS reservado e portabilidade (M3+)

- Teste final exatamente uma vez por versão congelada da hipótese.
- Transferência BTC→ETH e venue A→B somente com equivalência de regras/custos e sem olhar o destino durante tuning.
- Paper replay para conferir pipeline; paper forward para coletar novas observações em tempo real simulado. Qualquer retuning após analisar resultados forward cria nova versão de família e não reutiliza credenciais de OOS.

### Nível S4 — capacidade e operações (quando necessário)

- Se um backtest longo demorar demais, **medir** I/O, parsing, memória, consultas DuckDB, tempo do simulador e geração de relatórios em amostra real.
- Melhorias possíveis na ordem: processamento lazy, filtros pushdown, arquivo mensal compacto, pré-cálculo de features por snapshot, caching **versionado**, paralelizar experimentos independentes com limite de memória, GPU **só** se benefício medido.
- Migrar para DB distribuído apenas quando query/ingest local se tornar gargalo documentado. Não inferir que “mais CPU/GPU” aumenta validade estatística.

## F. Escala de dados e metas de capacidade (estimativas, não benchmarks)

- Um instrumento negociado 24/7 fornece ~525.600 minutos num ano comum. Dois instrumentos entre 2020-01 e 2026-09 somam ordem de 7 milhões de barras 1m **se** cobertura contínua existir.
- Com 2 símbolos, Parquet mensal, DuckDB e SSD, esse volume é uma base de engenharia **plausivelmente acessível em um PC doméstico**, mas a RAM e duração reais são desconhecidas até medir.
- Trades tick, book snapshots/deltas e features enriquecidas podem elevar armazenamento em ordens de grandeza; estimar por **um dia/mês real e faixa de expansão**, nunca números milagrosos.
- Guardar dataset bruto e backup do catálogo, mas separar backup de fonte rebaixável de feeds caros/não rebaixáveis. Sem criar política de retenção que viole licença.

## G. Controle de custo de desenvolvimento/agentes

**Objetivo do operador de agentes:** evitar gastar tokens, downloads e esforço com validação redundante sem deixar dinheiro/tempo inseguros.

- F0–F4 em sequências curtas com demo visível; uma sessão/fase e handoff.
- Modelos High para implementação habitual; Extra High nas decisões de causalidade, contabilidade, desduplicação de eventos e estatística.
- Registrar somente gargalos reais e bugs detectados; um agente de revisão read-only pode ser usado uma vez nas fases sensíveis, sem fila interminável de agentes discutindo arquitetura.
- Quando houver lentidão, medir um único recorte representativo e corrigir a camada dominante antes de comparar 10 bibliotecas.
- Status de release deve refletir o que **funciona na prática**, não quantidade de linhas de código produzidas.

**Risco de execução mínimo aceitável:** nenhum uso de capital real, nenhuma alegação de lucro comprovado, nenhum fill temporalmente impossível, nenhum trade duplicado e nenhum dataset reprovado escondido pelo painel.
