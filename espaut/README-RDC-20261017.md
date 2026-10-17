# ESPAUT — Correções executadas via Remote Desktop Commander

Data: 2026-10-17. Base: `7dd7156946d2385ecff8c9d209b97fb42d5bb868`.
Branch isolada: `fix/espaut-verified-gates`.

## Resumo das correções

1. `gh_ops.py`: `gh pr checks` mantém JSON válido mesmo com exit code de check falho ou pendente; erro de transporte/API gera exceção e não vira lista vazia. `pr_create` exige identidade confirmada. `pr_merge` não usa `--yes` inexistente. REST usa paginação e GraphQL lê `isResolved` e `isOutdated` das conversas; dados incompletos bloqueiam.
2. `security.py`: argumentos usam `subprocess.run` sem `shell=True`; caracteres comuns em nomes, prompts e comentários não são tratados como comandos shell. Byte NUL continua rejeitado.
3. `git_ops.py`: `status` e `diff --cached` suportam nomes com espaços e caracteres especiais via `-z`. Push identifica a branch explicitamente.
4. `preservation.py`: recusa commits com arquivos previamente staged fora do escopo, operações Git incompletas, branches protegidas e segredos reconhecidos no diff. Push só é sincronizado após comparação de SHAs.
5. `cli.py`: códigos de saída dos comandos refletem falhas; `merge-check` consulta head do PR, reconfirma que não mudou e lê os status C1–C8 corretamente. `session` executa comando acompanhado e verifica alterações ao sair. `task create` cria branch isolada quando iniciado na main limpa.
6. `governance.py`: C6 exige evidência de critérios de aceitação para a revisão atual; C8 requer revisão vinculada e C3 considera política de CI. Revisão com `CHANGES_REQUESTED` bloqueia.
7. `review.py`: IDs de pendências determinísticos; ledger conserva solicitações não resolvidas. Comentários indeterminados não desaparecem; resolução remota de threads não equivale a validação técnica automática.
8. `correction.py`: agente local Codex/Claude opcional e condicionado a scope, branch não protegida, árvore limpa, texto sem segredos reconhecidos e testes configurados. Alterações fora do scope ou verificações falhas não são enviadas automaticamente.
9. `schemas/`: ampliados para os campos de revisão, verificação de aceitação e comando de testes.

## Como usar

Pré-requisitos: Python 3.12+, Git e GitHub CLI autenticado. Para correção assistida, Codex ou Claude Code instalado/autenticado localmente.

```bash
python3 -m espaut init
python3 -m espaut task create "Implementar X" --objective "Critérios específicos"
# Configure .espaut/config.json: auto_commit=true e eligible_patterns não vazio.
python3 -m espaut session --task TASK-IDENTIFICADOR -- codex exec -s workspace-write "Implemente X"
python3 -m espaut pr create --task TASK-IDENTIFICADOR --title "Implementar X"
python3 -m espaut checks --task TASK-IDENTIFICADOR
python3 -m espaut reviews --task TASK-IDENTIFICADOR
python3 -m espaut pendencies --task TASK-IDENTIFICADOR
python3 -m espaut merge-check --task TASK-IDENTIFICADOR
```

`session` é um wrapper: só acompanha comandos executados por ele. **Não intercepta automaticamente todas as sessões já abertas no VS Code.** Se `auto_commit=false`, registra e apresenta a pendência, sem fazer commit. Se `auto_commit=true`, exige `eligible_patterns` explícitos e bloqueia push em main/master.

`correct --agent codex|claude --confirm` exige permissão explícita `ai_enabled=true` e `ai_sharing_authorized=true`, uma pendência previamente coletada, escopo e `correction_test_command` em `.espaut/config.json`. Sem essas condições, recusa a modificação. Os agentes usam a autenticação instalada, sem API paga própria adicionada pelo ESPAUT. Correções assistidas são processadas primeiro em um worktree Git temporário isolado; apenas um patch previamente validado e aprovado pelo controle de escopo pode ser aplicado ao checkout original.

## Evidências

O script de verificação reproduzível está fora do repositório, em
`/home/davis/projetos/NotebookLM/runs/ESPAUT-CORRECAO-20261017/run_suite.py`.

O relatório JSON de execução e os logs individuais estão em
`/home/davis/projetos/NotebookLM/runs/ESPAUT-CORRECAO-20261017/evidence/`.

Os testes `tests/espaut/test_espa_regressions.py` e
`tests/espaut/test_session_integration.py` exercitam regressões e fluxos
com remoto Git temporário, respectivamente.

## Limites comprovados

- Sem PR real ou workflow GitHub Actions usado na homologação: consultas foram verificadas por testes com retornos controlados.
- A execução real do Codex ou Claude Code **para produzir uma correção** não foi homologada; foi validado o contrato do adaptador e seus bloqueios.
- Varredura de segredos por regex não garante detecção de todas as credenciais e não substitui DLP abrangente.
- A leitura GraphQL falha de forma segura quando uma conversa ou PR exige paginação superior à implementada.
- A ferramenta não executa merge automático e não foi autorizada a enviar a branch de correção ao GitHub.
- Um resultado PASS da suíte Python **não significa DONE global** dos 33 cenários EARS sem validação dos cenários externos.

Nenhum outro projeto existente do usuário deve ser modificado como consequência deste trabalho.
