import time
from joblib import Parallel, delayed
from multiprocessing import Manager

import logging
logging.basicConfig(filename='query_graph.log', encoding='utf-8', level=logging.DEBUG)

import spacy

from researcher import Researcher
from sentence_transformers import SentenceTransformer, util

import os
os.environ['TOKENIZERS_PARALLELISM'] = "true"

import torch
# device = torch.device("cuda" if torch.cuda.is_available() else "mps" if  torch.backends.mps.is_available() else "cpu")


def get_relevance(sentence):
    return -sentence.relevance

def get_urls(researcher, parallelize_get_urls=True):
    ### parallelized url retrevial
    logging.debug("starting URL retrevial")
    manager = Manager()
    url_dict = manager.dict()

    # Run the parallel jobs, passing the shared dictionary as an argument
    start_time = time.perf_counter()
    # search_queries = researcher.search_queries
    if parallelize_get_urls:
        Parallel(n_jobs=-1, backend="loky")(delayed(researcher.get_urls)(search_query, url_dict) for search_query in researcher.search_queries)
    else:
        for search_query in researcher.search_queries:
            researcher.get_urls(search_query, url_dict)
    finish_time = time.perf_counter()
    researcher.urls = url_dict
    del url_dict

    logging.debug(f"Gathered URL's in {finish_time-start_time} seconds")

def get_sentences_and_pages(researcher, model, parallelize_create_pages=True):
    ### parallelized page creation
    logging.debug("starting page and sentences creation")
    manager = Manager()
    sentences_list = manager.list([])

    # Run the parallel jobs, passing the shared dictionary as an argument # search_queries, url, pages_dict
    start_time = time.perf_counter()
    if parallelize_create_pages:
        # create_pages_and_sentences(self, search_queries, url, sentence_list, model)
        Parallel(n_jobs=4, backend="loky")(delayed(researcher.create_pages_and_sentences)(researcher.urls[url], url, sentences_list, model) for url in researcher.urls)
    else:
        for url in researcher.urls:
            researcher.create_pages_and_sentences(researcher.urls[url], url, sentences_list, model)
    finish_time = time.perf_counter()
    logging.debug(f"Created sentences in {finish_time-start_time} seconds")

    logging.debug("sorting sentences")
    start_time = time.perf_counter()
    sentences_list.sort(key = get_relevance) # lambda function causes pickling error
    researcher.nodes = sentences_list[:min(researcher.num_nodes, len(sentences_list))]
    # del sentences_list
    finish_time = time.perf_counter()
    logging.debug(f"sorted nodes by similarity in {finish_time-start_time} seconds")

def pipeline(query, num_nodes=50, parallelize_get_urls=True, parallelize_create_pages=True):
    model = SentenceTransformer('multi-qa-MiniLM-L6-cos-v1', device="cuda" if torch.cuda.is_available() else "mps" if  torch.backends.mps.is_available() else "cpu")
    nlp = spacy.load("en_core_web_sm")
    researcher = Researcher(query, nlp=nlp, num_nodes=num_nodes)
    researcher.gpt_response_embedding = model.encode(researcher.gpt_response)
    researcher.gpt_sentences_embedding = model.encode(researcher.gpt_sentences)
    print("ChatGPT: " + researcher.gpt_response)

    get_urls(researcher, parallelize_get_urls=parallelize_get_urls)
    get_sentences_and_pages(researcher, model, parallelize_create_pages=parallelize_create_pages)
    
    # calculate which gpt sentences are most similar to each node
    researcher.gpt_sentences_embedding = model.encode(researcher.gpt_sentences)
    for node in researcher.nodes:
        node.gpt_relevance_by_sentence = util.dot_score(node.embedding, researcher.gpt_sentences_embedding)
        mean = node.gpt_relevance_by_sentence.mean()
        std = node.gpt_relevance_by_sentence.std()
        node.relevant_sentences = node.gpt_relevance_by_sentence > mean + std

    return researcher
    
def ordinal(n):
    if str(n)[-1] == '1':
        return str(n) + 'st'
    elif str(n)[-1] == '2':
        return str(n) + 'nd'
    elif str(n)[-1] == '3':
        return str(n) + 'rd'
    else:
        return str(n) + 'th'

if __name__ == "__main__":
    start = time.perf_counter()
    query = input("Enter query: ")
    if query == "":
        query = "Is it true that Neil Armstrong never went to space and that he was a paid actor by the inner circle, AKA NASA?"
    logging.debug("starting new run with query: " + query)
    
    num_nodes = input("Enter number of nodes (usually on order of 25-150): ")
    try:
        num_nodes = int(num_nodes)
    except:
        num_nodes = 25
    print("Asking ChatGPT for response...")

    researcher = pipeline(query, num_nodes=num_nodes)
    end = time.perf_counter()
    logging.info(f"finished in {end-start} seconds")

    # output
    print("\n\nContent from the Web (obtained via Google Search):")
    for (index, node) in enumerate(researcher.nodes):
        # print(node.relation_to_gpt)
        print(f"{ordinal(index+1)} most similar sentence to ChatGPT's response. Relevance to ChatGPT's response: ", node.relevance)
        print(node.context)
        print("Most relevant ChatGPT sentences:")
        print([researcher.gpt_sentences[idx] for (idx, val) in enumerate(node.relevant_sentences[0]) if val.item()])
        print("\n\n")