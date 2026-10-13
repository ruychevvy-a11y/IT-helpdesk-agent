from helpdesk.tools import access, account, knowledge, ticket

# Agent 的全部工具
ALL_TOOLS = (
    account.ACCOUNT_TOOLS
    + access.ACCESS_TOOLS
    + ticket.TICKET_TOOLS
    + knowledge.KNOWLEDGE_TOOLS
)
