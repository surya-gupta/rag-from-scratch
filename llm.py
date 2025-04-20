from langgraph.graph import StateGraph, END
from langgraph.pregel import Pregel
from typing import List, Dict, Any
from openai import OpenAI
import asyncio
from openevals import EvaluationHarness, RagEvaluationPrompt

# ----------- CONFIG -----------
BATCH_SIZE = 3
MAX_RETRIES = 3

# ----------- UTILITIES -----------
def chunk_list(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]

# ----------- LLM CLIENT -----------
client = OpenAI()

async def call_llm(prompt: str, context: str) -> str:
    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "You are an auditing assistant."},
            {"role": "user", "content": f"Context: {context}\nPrompt: {prompt}"}
        ]
    )
    return response.choices[0].message.content

# ----------- LANGGRAPH NODES -----------

def batcher_node(state: Dict[str, Any]) -> Dict[str, Any]:
    auditChecklist = state['auditChecklist']
    n = state['batch_size']
    chunks = chunk_list(auditChecklist, n)
    batch_prompts = ["\n".join(chunk) for chunk in chunks]
    return {**state, "batches": batch_prompts, "batch_results": [None] * len(batch_prompts), "retry_indices": list(range(len(batch_prompts)))}

async def executor_node(state: Dict[str, Any]) -> Dict[str, Any]:
    context = state['context']
    batches = state['batches']
    retry_indices = state['retry_indices']
    results = state['batch_results']

    tasks = [call_llm(batches[i], context) for i in retry_indices]
    responses = await asyncio.gather(*tasks)

    for i, idx in enumerate(retry_indices):
        results[idx] = responses[i]

    return {**state, "batch_results": results}

def evaluator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    evaluator = EvaluationHarness(model="gpt-4")
    failed = []
    for i, res in enumerate(state['batch_results']):
        prompt = RagEvaluationPrompt(
            query=state['batches'][i],
            context=state['context'],
            response=res
        )
        eval_result = evaluator.evaluate(prompt)
        if "FAIL" in eval_result.upper():
            failed.append(i)
    return {**state, "retry_indices": failed}

def retry_handler_node(state: Dict[str, Any]) -> str:
    if not state['retry_indices']:
        return "AGGREGATE"
    if state['retries'] >= MAX_RETRIES:
        return "AGGREGATE"
    return "RETRY"

def retry_node(state: Dict[str, Any]) -> Dict[str, Any]:
    return {**state, "retries": state['retries'] + 1}

def aggregate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    final_result = "\n---\n".join(state['batch_results'])
    return {"result": final_result}

# ----------- GRAPH DEFINITION -----------
graph = StateGraph()
graph.add_node("BATCHER", batcher_node)
graph.add_node("EXECUTOR", executor_node)
graph.add_node("EVALUATOR", evaluator_node)
graph.add_node("RETRY_HANDLER", retry_handler_node)
graph.add_node("RETRY", retry_node)
graph.add_node("AGGREGATE", aggregate_node)

graph.set_entry_point("BATCHER")
graph.add_edge("BATCHER", "EXECUTOR")
graph.add_edge("EXECUTOR", "EVALUATOR")
graph.add_edge("EVALUATOR", "RETRY_HANDLER")
graph.add_conditional_edges("RETRY_HANDLER", {
    "RETRY": "RETRY",
    "AGGREGATE": "AGGREGATE"
})
graph.add_edge("RETRY", "EXECUTOR")
graph.add_edge("AGGREGATE", END)

flow = graph.compile()

# ----------- RUN EXAMPLE -----------
# Run this block using: asyncio.run(run())
async def run():
    input_state = {
        "auditChecklist": [f"Checklist item {i+1}" for i in range(20)],
        "context": "Sample context for auditing.",
        "batch_size": BATCH_SIZE,
        "retries": 0
    }
    result = await flow.invoke(input_state)
    print("\nFinal aggregated result:\n")
    print(result['result'])
