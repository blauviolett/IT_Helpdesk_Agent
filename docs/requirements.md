Agentic AI Engineer Take-Home Project

Candidate Instructions — IT Helpdesk Agent

Thank you for taking the time to complete this exercise.

The purpose of this assignment is to understand how you design and build an agentic AI system for a real user problem. We are less interested in polished UI or framework-specific tricks, and more interested in your engineering judgment, product thinking, and ability to build a practical, reliable system.

Business Problem

Traditional IT support forces employees through a frustrating workflow: file a ticket, wait for assignment, wait for a human agent to investigate, go back and forth for details, and eventually get a resolution — often hours or days later. Most issues (password resets, VPN problems, software access) are repetitive and well-documented, yet every one still requires human processing time.

Your task is to build an AI-powered IT support agent that replaces the traditional ticket system. Instead of filing a ticket and waiting, employees interact directly with an AI agent that:

understands their problem through conversation
investigates using internal knowledge bases, system status, and historical data
resolves the issue directly when possible (e.g., providing step-by-step instructions, looking up the right information)
escalates to a human IT specialist only when the issue exceeds the agent's capabilities — handing off a complete context summary so the employee doesn't have to repeat themselves
The goal is to eliminate the ticket queue for the majority of common IT issues and dramatically reduce resolution time from hours to minutes.

Product Goal

Design and implement a conversational IT support agent that serves as the employee's first and primary point of contact for IT issues. The agent should be able to hold a multi-turn conversation, diagnose problems, resolve what it can, and seamlessly escalate what it can't.

The solution should feel like talking to a knowledgeable IT specialist — not like filling out a form.

Core Use Case

Build a system where an employee describes their IT problem in natural language, and the agent drives the resolution process: asking clarifying questions, pulling information from backend systems, walking the employee through fixes, and escalating only when necessary.

Your system should handle conversations such as:

Password / account issues: "I can't log into Okta. I've tried resetting my password but it still doesn't work. I need access urgently for a client meeting in 30 minutes."
Software / application issues: "Salesforce has been loading extremely slowly since yesterday. My teammates in the Chicago office are seeing the same thing."
Hardware / connectivity: "My VPN keeps disconnecting every 10–15 minutes. I'm working remotely and can't access internal tools."
Access / permissions requests: "I just joined the Data Engineering team and need access to the Snowflake production database and the internal Grafana dashboards."
Complex / multi-system issues: "Since the IT maintenance window last Friday, our team's automated data pipeline has been failing. The Jenkins jobs time out and the downstream Tableau reports are stale."
Data & Environment

You should simulate or mock the backend data sources the agent would query. Suggested sources include:

Source	Description	Example
Knowledge base	IT articles, FAQs, runbooks, troubleshooting guides	Markdown or text documents
System status	Current service health, known outages, recent change logs	Structured data (JSON/API)
User directory	Employee info — department, role, location, assigned equipment, access permissions	Mock user records
Resolution history	Past resolved issues for similar problems	Searchable archive
Policy / rules	What the agent is authorized to do vs. what requires human approval	Structured rules
You should create realistic synthetic data for at least 2–3 of these sources. The data does not need to be large, but it should be rich enough to demonstrate multi-source reasoning.

Problem Framing Requirements

Your submission should clearly define:
- Who the user is: the employee with an IT problem (not a helpdesk agent)
- What interaction model you chose: how the employee communicates with the agent (e.g., chat interface, CLI, API)
- What the agent can resolve directly vs. what it escalates to a human
- What data sources the agent queries and how it uses them
- What a successful interaction looks like from the employee's perspective (e.g., problem resolved in under 2 minutes without filing a ticket)
- What escalation looks like: how the agent hands off to a human with full context so the employee doesn't start over

What We Are Looking For

We would like your submission to demonstrate, as appropriate to your design. Note that this project requires thoughtful design and iterative engineering — a simple one-shot prompt or vibe-coding approach will not produce a sufficient solution:

Conversational diagnosis: The agent should ask clarifying questions, not just take a single input. It should drive the conversation toward resolution.
Multi-step reasoning: The agent should gather information from multiple sources, form hypotheses, and refine its diagnosis — not just do a single lookup.
Tool use: The agent should invoke tools (KB search, status check, user lookup, etc.) as part of its reasoning workflow.
State / memory: The agent should track the conversation context, what it has already investigated, and what remains unresolved across multiple turns.
Resolution vs. escalation judgment: The agent should know the boundary between what it can handle and what needs a human, and should escalate with a complete summary.
Reliability: The agent should handle vague descriptions, missing information, conflicting data, and tool failures gracefully.
Safety / guardrails: The agent should not fabricate solutions, should not perform actions beyond its authority, and should be transparent about its confidence level.
Evaluation: You should have a deliberate way to assess whether the agent's resolutions are correct and its escalation decisions are appropriate.
Production thinking: Consider observability, cost, latency, and maintainability.
You do not need to solve every dimension perfectly. We care more about the quality of your tradeoffs than about maximizing feature count.

Deliverables

Please submit the following:

Code repository

A runnable project with clear setup instructions.

README

Please include:
- the IT support problem you chose to solve and why
- why an agentic approach is needed (vs. a simple chatbot, FAQ search, or rule engine)
- your architecture and design decisions
- how you handle the resolution vs. escalation boundary
- the data sources you simulated and why
- your assumptions and tradeoffs
- how to run the system
- how you evaluated it
- what you would improve with more time

Demo

Be prepared to walk us through your solution during the interview. A live demo is preferred, but a recorded demo is acceptable if needed. We will type in new IT problems during the interview to see how the agent handles them.

Optional design note

You may include a short note on how you would productionize this — integration with Slack/Teams for the chat interface, SSO for user identification, audit logging, handoff to ServiceNow for escalated issues, etc.

Technical Guidance

You may use any frameworks, libraries, models, or APIs you prefer
The solution should be runnable locally
Keep external dependencies reasonable and clearly documented
Focus on engineering clarity rather than building the broadest possible feature set
Interview Session

During the interview, we will ask you to walk through:
- the IT support problem and scope you chose
- your architecture and implementation
- a live demo where we type in IT problems and converse with the agent
- how the agent handles ambiguous, incomplete, or out-of-scope requests
- how escalation works and what context the agent preserves for the human
- evaluation results and failure cases
- how you would improve and productionize the system

What Good Submissions Usually Show

We are not looking for an overly polished product. A well-scoped, thoughtful solution is better than a large but fragile one.

Strong submissions typically:
- frame a clear employee-facing IT support experience
- design a conversational diagnostic workflow (not just a single LLM call)
- show the agent asking clarifying questions and driving toward resolution
- orchestrate multiple tools meaningfully (search KB, check status, look up user, etc.)
- demonstrate clear resolution vs. escalation judgment
- handle edge cases — vague descriptions, missing articles, conflicting information
- evaluate with concrete test conversations and expected outcomes
- explain tradeoffs candidly, including what was scoped out and why

We are excited to see how you approach the problem!