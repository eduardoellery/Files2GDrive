# Interface web local

## Iniciar

Com a CLI/instância anterior parada, executar no projeto:

```bash
.venv/bin/python -m files2gdrive --state-dir /caminho/estado serve
```

O servidor abre o navegador com um link de acesso temporário, atende somente em `127.0.0.1` e mantém o motor funcionando quando a página é fechada. `Ctrl+C` no terminal encerra servidor e motor, preservando fila e prazos. O encerramento espera o worker terminar para não liberar o estado enquanto ele ainda trabalha.

- Porta padrão: 8765. Alterar com `--port 8766`; usar `--port 0` para atribuição automática.
- `--no-browser` inicia sem abrir uma janela. O link privado de inicialização fica em `web-launch-url` dentro do estado, com modo 0600. Esse arquivo não contém tokens Google, mas dá acesso à interface local: não compartilhar.
- Apenas uma instância/CLI de alteração pode usar o mesmo estado. Usar os controles da página enquanto o servidor está executando.
- O cliente OAuth precisa estar configurado no desenvolvimento. O pacote pode trazer a identificação do cliente do distribuidor; cada usuário autentica sua própria conta pelo navegador. Não há campo para colar tokens ou credenciais na página.
- Um estado novo sem configuração Google usa explicitamente o simulador; a interface sinaliza esse modo. O estado real já configurado mantém backend Drive.

## Telas disponíveis

- **Visão geral:** autenticação explícita, estado, pausa/retomada, fila e progresso. “Atualizar painel” consulta o estado local; “Verificar agora” solicita nova conferência dos arquivos.
- **Pastas:** navegação pela pasta pessoal com checkboxes, seleção recursiva, exceções por subpasta, exibição opcional de ocultos e revisão antes de aplicar. Retiradas do backup seguem a retenção de 30 dias.
- **Filtros:** regras globais e por pasta, exemplos de curingas, prévia obrigatória e revalidação antes de salvar. Prévia expira em dez minutos ou se os arquivos/efeitos mudarem.
- **Atividade:** consulta por pasta, nível e data. A tela limita a mil registros; exportação inclui todos os registros retidos que correspondem ao filtro.
- **Recuperar:** catálogo, seleção dos arquivos ativos/retidos e restauração em pasta nova, sem sobrescrita ou monitoramento automático. A reconexão após reinstalação continua pela CLI nesta etapa.
- **Configurações:** conta e nomes de destino/computador. Mudança de destino após o primeiro backup ainda é recusada para evitar mover/misturar backups sem uma migração definida.

Adicionar uma pasta dá início ao backup dos arquivos já existentes assim que o motor estiver conectado e não estiver pausado. Nenhuma pasta é cadastrada automaticamente ao abrir a interface.

## Autenticação corporativa

Se Google/cofre exigir nova autorização, o estado muda para autenticação pendente, sem perder fila e sem alterar pausa manual. O usuário inicia o login pela página e o conclui no navegador Google. Não programar login diário fixo: o comportamento depende da política efetiva do Workspace.

O motor continua reconciliando o estado local quando o acesso remoto está indisponível. Falhas temporárias de conexão são verificadas novamente; exigência de nova autorização aguarda ação do usuário. A conta real corporativa e sua política diária ainda não foram testadas.

## Segurança e execução

- Host e origem exatos em loopback; sem CORS, proxy confiável ou escuta na rede local.
- Link inicial aleatório de uso único, cookie HttpOnly/SameSite e token CSRF nas alterações.
- CSP, proteção de enquadramento, respostas sem cache e arquivos estáticos com caminhos fixos.
- Requisições JSON limitadas a 64 KiB; tokens Google nunca são expostos no frontend.
- SQLite e o motor pertencem a um único worker. A página consulta snapshots de memória durante operações demoradas.
- Comandos são serializados pelo controlador. Até quatro envios podem estar em andamento; pausa e saída são atendidas entre grupos. Login, revisão de pastas e restauração aparecem como tarefas em andamento.
- Bandeja, notificações e inicialização automática estão descritas em `INTEGRACAO-DESKTOP.md`; instalação em `INSTALACAO-DEB.md`. Fechar a página não encerra o processo.

## Validação

A suíte inclui testes de sessão, CSRF, Host/Origin, seleção de pastas, filtros, tarefas em segundo plano, reautenticação, encerramento e exportação. Os testes usam perfis temporários e armazenamento simulado; não precisam acessar uma conta Google real.
