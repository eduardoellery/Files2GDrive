# Files2GDrive — requisitos

Atualizado em: 2026-11-01.

## Objetivo

Construir uma aplicação para Ubuntu que monitore arquivos locais continuamente e os envie ao Google Drive, com configuração gráfica e distribuição por instalador `.deb`.

## Requisitos solicitados

- Detectar novos arquivos nas pastas monitoradas e enviá-los ao Google Drive automaticamente.
- Ao excluir um arquivo local, manter sua cópia no Google Drive por pelo menos 30 dias antes de excluí-la de lá.
- Disponibilizar ícone na bandeja do sistema.
- Menu da bandeja por clique direito com Configuração, Pausar backup, Retomar backup e Sair. Clique duplo dispensado na rodada 8.
- Permitir autenticação/integração com Google Drive na configuração. Uma opção adicional no menu da bandeja é uma possibilidade a avaliar.
- Permitir selecionar uma ou mais pastas locais para monitoramento e backup.
- Permitir escolher o nome da pasta de destino no Google Drive.
- Entregar instalador `.deb` compartilhável.

## Em aberto

- Identificação do ambiente gráfico no Ubuntu 24.04 e compatibilidade da bandeja.
- Quantidade aproximada de arquivos.
- Recuperação após falta de internet, reinício e falhas parciais.
- Tratamento técnico de arquivos ainda em gravação e formato de preservação dos metadados Linux.
- Formato técnico dos metadados remotos e detalhes de restauração segura.
- Detalhamento da arquitetura aprovada, autenticação, segurança, testes e distribuição.

## Comportamento confirmado — rodada 1

- Backup unidirecional: Ubuntu → Google Drive.
- Edições locais atualizam a cópia remota; inicialmente não criar arquivos separados para cada versão.
- Exclusões feitas diretamente no Drive são respeitadas: não restaurar automaticamente apenas por detectar a exclusão. A recuperação fica a cargo de alguém pela lixeira do Drive.
- Exclusões locais movem a cópia remota para uma área chamada `Excluídos`; após retenção mínima de 30 dias, enviar à lixeira do Drive, sem exclusão definitiva pelo aplicativo.
- Cada pasta monitorada terá uma subpasta no destino, preservando sua estrutura interna.

## Comportamento confirmado — rodada 2

- Nova edição local reenvia arquivo excluído remotamente e pode sobrescrever edições remotas.
- A retenção começa na detecção da exclusão local; limpeza vencida é executada quando o aplicativo voltar a funcionar com internet.
- Reaparecimento local durante a retenção restaura o arquivo da área `Excluídos`, atualiza o conteúdo e cancela a exclusão agendada.
- Inicialização automática ao entrar na sessão; execução independente da página de configuração estar aberta.
- Alvo inicial: Ubuntu 24.04; ambiente gráfico a identificar.

## Comportamento confirmado — rodada 3

- Aproximadamente 100 GB de dados, com arquivos individuais de até poucos gigabytes.
- Uma conta Google por instalação; pastas de origem apenas no disco interno.
- Enviar arquivos existentes ao cadastrar uma pasta.
- Oferecer pausa/retomada, fila, progresso, consulta de erros e retomada automática após falta de internet.
- Permitir backup de pastas que começam com ponto, incluindo pastas de configuração do Linux.

## Comportamento confirmado — rodada 4

- Incluir itens ocultos dentro das pastas selecionadas.
- Permitir exclusões configuráveis por subpasta ou padrão, sem exclusões silenciosas por padrão.
- Ignorar links simbólicos e informar na interface.
- Preservar permissões Linux e bit executável para futura restauração.
- Destino no Meu Drive da conta.

## Comportamento confirmado — rodada 5

- Restauração pelo aplicativo na primeira versão, com destino escolhido, reaplicação de permissões e sem sobrescrita por padrão.
- Arquivos acessíveis individualmente no Drive; permissões em metadados auxiliares.
- Refletir renomeações e movimentos dentro da mesma pasta monitorada.
- Remover uma pasta da configuração inicia a retenção de 30 dias de seu backup (`Excluídos` → lixeira).
- Raiz desaparecida ou inacessível provoca pausa e aviso, sem exclusão automática de todo o backup.
- Disponibilizar log local das operações para consulta.

## Comportamento confirmado — rodada 6

- Logs consultáveis na configuração, filtráveis por data, pasta e nível, com exportação local.
- Registrar horário, operação, caminho, resultado e erro, sem conteúdo de arquivos ou credenciais.
- Rotação dos logs: até 30 dias e no máximo 100 MB, descartando registros mais antigos ao atingir qualquer limite.
- Recadastro durante a retenção reativa e reconcilia o backup; cancela exclusão dos arquivos presentes localmente, mantendo o prazo dos ausentes.
- Remoção da configuração não reinicia prazos de itens já em `Excluídos`; itens ativos iniciam seus 30 dias.

## Comportamento confirmado — rodada 7

- Agrupar alterações durante alguns segundos antes do envio.
- Pausa suspende envios e limpeza, sem interromper os prazos de retenção.
- Interface web somente local.
- Separar backups por computador ao usar uma mesma conta.
- Configuração inicial com autenticação na conta Google de cada usuário; sem configuração de projeto ou chaves pelo destinatário do `.deb`.
- Stack pesquisada e aprovada na rodada 8.

## Comportamento confirmado — rodada 8

- Python 3.12, watchdog, SQLite, FastAPI/Uvicorn, HTML/CSS/JavaScript e helper PyGObject/Ayatana; distribuição `.deb`.
- Organização remota: `<Pasta escolhida>/<Computador>/<Pasta monitorada>/...`, criada pelo aplicativo.
- Efeito de Sair e persistência da pausa definidos na rodada 9.

## Comportamento confirmado — rodada 9

- Sair encerra motor e bandeja, mantendo fila e prazos; aplicativo pode reiniciar ao ser aberto ou na próxima sessão.
- Pausa persiste entre reinícios e reaberturas até retomada explícita.
- Filtros novos iniciam retenção das cópias já enviadas, com efeito informado ao salvar; não reiniciar prazos existentes.
- Preservar fila e avisar na interface e por notificação Ubuntu em falta de espaço ou reautenticação, sem repetição contínua.
- Permitir exclusão por extensão e wildcards `*` e `?`.

## Comportamento confirmado — rodada 10

- Filtros globais e por pasta combinados por exclusão.
- Padrões por nome em qualquer nível quando não contêm `/`; padrões de caminho relativos à raiz quando contêm `/`.
- Wildcards: `*` para zero ou mais caracteres, `?` para um, sem atravessar `/`; `**` atravessa níveis. Barra final indica pasta e seu conteúdo.
- Diferenciar maiúsculas/minúsculas por padrão, com opção de ignorar a diferença; seleção por extensão gera padrão equivalente.
- Prévia antes de salvar; remoção de filtro reconcilia os arquivos liberados e cancela retenção quando aplicável, respeitando outros filtros.

## Comportamento confirmado — rodada 11

- Após reinstalação, criar novo backup ou reconectar explicitamente um existente, mapeando e conferindo pastas antes de inferir exclusões.
- Restaurar itens ativos e em `Excluídos`, por padrão em pasta separada e sem monitoramento automático. Lixeira permanece com recuperação manual pelo Drive.
- Preservar permissões comuns e data de modificação; proprietário/grupo, ACLs, atributos estendidos e bits especiais fora da primeira versão.
- Nomes exibidos configuráveis para computador e destinos, com identificadores internos persistentes.

## Comportamento confirmado — rodada 12

- Não permitir raízes monitoradas sobrepostas.
- Movimentos entre raízes criam item no destino e iniciam retenção da cópia na origem; movimentos dentro de uma raiz são refletidos diretamente.
- Sem histórico próprio de versões na primeira versão. Retenção de exclusões fixa em 30 dias.
- Atualizações manuais via novo `.deb`; sem atualizador automático.

## Processo de definição

Requisitos e arquitetura são documentados nos guias públicos do projeto. Discussões e relatórios locais de desenvolvimento não fazem parte da distribuição do código. Mudanças de comportamento precisam atualizar a documentação e os testes correspondentes.

## Compatibilidade com ambientes corporativos

- Suportar Google Workspace corporativo, inclusive organizações que exijam reautenticação frequente.
- Identificar em teste se a regra afeta somente a sessão web ou também a autorização OAuth do Drive; não presumir expiração diária do refresh token.
- Quando nova autorização for exigida, preservar fila e prazos, suspender operações remotas, indicar necessidade de login e retomar após autorização, respeitando pausa manual.
- Validar liberação do cliente OAuth pelas políticas do administrador do Workspace.
