import re
import threading

import requests
from bs4 import BeautifulSoup
import json

import torch
from transformers import BertTokenizer, BertModel
from transformers import pipeline
from scipy.spatial.distance import cosine

import spacy 

from information_extraction.gpt import callGPT
import config

class Researcher:
    # Load pre-trained model (weights)
    model = BertModel.from_pretrained('bert-base-uncased',
                                    output_hidden_states = True, # Whether the model returns all hidden-states.
                                    )
    # Put the model in "evaluation" mode, meaning feed-forward operation.
    model.eval()
    # Load pre-trained model tokenizer (vocabulary)
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

    classifier = pipeline('zero-shot-classification', model='roberta-large-mnli')
    nlp = spacy.load("en_core_web_sm")

    def __init__(self, query, num_results=10, context_window=2):
        self.query = query
        self.query_vector = self.embedding(self.query)

        self.gpt_response = self.ask_gpt_query(query)
        self.gpt_sentences = Page.split_into_sentences(self, self.gpt_response) 
        self.parser = Parser(query, self.gpt_response, self.nlp, threshold=0.4)
        self.gpt_keywords = self.parser.keywords
        self.gpt_keyword_sentence_mapping = self.parser.gpt_keyword_sentence_mapping

        self.queries = self.parser.search_queries

        self.num_results = num_results
        self.context_window = context_window
        
        self.urls = dict()
        self.fetch_urls()

        self.pages = {}
        self.create_pages()

    def ask_gpt_query(self, query):
        with open("query_graph/information_extraction/gpt_prompts.json", "r") as f:
            prompt = json.loads(f.read())["initial prompt"]
        prompt += query
        response = callGPT(prompt)
        return response

        
    def fetch_urls(self):
        for search_query in self.queries:
            search = Search(search_query, self.num_results)
            for url in search.search_google():
                if url not in self.urls:
                    self.urls[url] = {search_query}
                else:
                    self.urls[url].add(search_query)

    def create_pages(self):
        def create_page(search_queries, url, pages):
            page = Page(search_queries,url)
            pages[url] = page

        threads = []
        for url in self.urls:
            search_queries = self.urls[url]
            threads.append(threading.Thread(target=create_page, args=(search_queries,url,self.pages)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def top_k_similar_sentences(self, k=100, resolution=10000):
        similarities = [[] for _ in range(resolution)]
        num_sentences = 0
        for page in self.pages:
            if self.pages[page].content:
                for (position, sentence) in enumerate(self.pages[page].sentences):
                    sent = Sentence(sentence, self.pages[page].get_sentence_content(position, self.context_window))
                    similarity = sent.similarity_to_query(self.query_vector)
                    similarities[int(similarity*resolution)].append(sent)
                    num_sentences += 1
        
        k = min(k, num_sentences)
        top_k = [""]*k
        count = 0
        for similarity in similarities:
            for sentence in similarity:
                if count == k:
                    break
                top_k[count] = sentence
                count += 1
        return top_k   

    def embedding(self, text, context=""):
        # Add the special tokens.
        marked_text = "[CLS] " + text + " [SEP]"
        
        # Split the sentence into tokens.
        tokenized_text = Researcher.tokenizer.tokenize(marked_text)

        # Map the token strings to their vocabulary indeces.
        indexed_tokens = Researcher.tokenizer.convert_tokens_to_ids(tokenized_text)

        # Mark each of the 22 tokens as belonging to sentence "1".
        segments_ids = [1] * len(tokenized_text)
        # Convert inputs to PyTorch tensors
        tokens_tensor = torch.tensor([indexed_tokens])
        segments_tensors = torch.tensor([segments_ids])

        # Run the text through BERT, and collect all of the hidden states produced
        # from all 12 layers. 
        with torch.no_grad():

            outputs = Researcher.model(tokens_tensor, segments_tensors)

            # Evaluating the model will return a different number of objects based on 
            # how it's  configured in the `from_pretrained` call earlier. In this case, 
            # becase we set `output_hidden_states = True`, the third item will be the 
            # hidden states from all layers. See the documentation for more details:
            # https://huggingface.co/transformers/model_doc/bert.html#bertmodel
            hidden_states = outputs[2]

        # `hidden_states` has shape [13 x 1 x 22 x 768]

        # `token_vecs` is a tensor with shape [22 x 768]
        token_vecs = hidden_states[-2][0]

        # Calculate the average of all 22 token vectors.
        sentence_embedding = torch.mean(token_vecs, dim=0)
        return sentence_embedding

class Parser(Researcher):
    def __init__(self, query, gpt_response, nlp, threshold=0.4):
        self.query = query
        self.gpt_response = gpt_response
        self.nlp = nlp
        self.threshold = threshold
        self.keywords, self.gpt_keyword_sentence_mapping = self.extract_keywords()
        self.search_queries = self.generate_search_queries()

    def get_sentence_index(self, phrase, sentences):
        """_summary_

        Args:
            phrase (spacy.span): phrase we want the sentence index of
            sentences (list): list of sentences (spans)

        Returns:
            int: index of phrase in sentences
        """
        for ix, sentence in enumerate(sentences):
            if sentence.end >= phrase.end:
                index = ix
                break
        return index

    def extract_keywords(self):
        """_summary_

        Args:
            input (string): query from which we want to extract keywords
            nlp (space.Language): a model (either off-the-shelf or custom) that creates Doc object

        Returns:
            list: list of strings, where each string is a noun phrase
        """
        gpt_doc = self.nlp(self.gpt_response)
        query_doc = self.nlp(self.query)
        gpt_keywords = list(gpt_doc.noun_chunks)
        query_keywords = list(query_doc.noun_chunks)
        gpt_sentences = list(gpt_doc.sents)

        keywords = set()
        gpt_keyword_sentence_mapping = dict()
        for gpt_word in gpt_keywords:
            max_similarity = 1
            most_similar = None
            for query_word in query_keywords:
                similarity = cosine(gpt_word.vector, query_word.vector)
                if similarity < max_similarity:
                    max_similarity = similarity
                    most_similar = query_word
            
            if max_similarity <= self.threshold:
                keywords.add((most_similar.text, gpt_word.text))
                # create keyword to sentence mapping
                position = self.get_sentence_index(gpt_word, gpt_sentences)
                gpt_keyword_sentence_mapping[gpt_word.text] = position
        return keywords, gpt_keyword_sentence_mapping

    def generate_search_queries(self):
        search_queries = set()
        for (q, cgpt) in self.keywords:
            search_queries.add(q + " AND " + cgpt)
            search_queries.add(q + " OR " + cgpt)
        return search_queries


class Search(Researcher):
    def __init__(self, search_query, num_results):
        self.search_query = search_query    
        self.num_results = num_results

    def search_google(self):
        """
        before is string in YYYY-MM-DD format
        """
        output = []
        while self.num_results > 0:
            if self.num_results < 10:
                page=1
                num=self.num_results
            else:
                num=10
                page=1
            params = {
                "key": config.GGLSEARCH_APIKEY(),
                "cx": config.GGL_SE(),
                "q": self.search_query,
                "h1": "en",
                "lr": "lang_en",
                "page": page,
                "num": num
            }

            response = requests.get(config.GGLSEARCH_URL(), params=params)
            assert (int(response.status_code) > 199 and int(response.status_code) < 300), "Google API Non-Responsive. Check search quotas. Error: " + str(response.status_code)
            response = json.loads(response.content)
            response["error"] = 0
            for item in response["items"]:
                output.append(item)
            self.num_results -= num
            page += 1

        return list(item["link"] for item in output)
    

class Page(Researcher):
    def __init__(self, search_queries, url): #content, url, ranking):
        self.search_queries = search_queries
        self.url = url

        self.content = self.get_webpage_content()
        if self.content:
            self.sentences = self.split_into_sentences(self.content)

    def get_webpage_content(self):
        try:
            # Send a GET request to the specified URL
            response = requests.get(self.url)

            # Check if the request was successful (status code 200)
            if response.status_code == 200:
                # Parse the HTML content of the webpage
                soup = BeautifulSoup(response.content, 'html.parser')

                # Extract the textual content from the parsed HTML
                # For example, if you want to get the text from all paragraphs:
                paragraphs = soup.find_all('p')
                content = ' '.join([p.get_text() for p in paragraphs])

                return content
            else:
                print("Request failed with status code:", response.status_code)
        except requests.RequestException as e:
            print("An error occurred:", str(e))

        return None


    def split_into_sentences(self, text: str) -> list[str]:
        """
        Split the text into sentences.

        If the text contains substrings "<prd>" or "<stop>", they would lead 
        to incorrect splitting because they are used as markers for splitting.

        :param text: text to be split into sentences
        :type text: str

        :return: list of sentences
        :rtype: list[str]
        """
        # -*- coding: utf-8 -*-
        alphabets= "([A-Za-z])"
        prefixes = "(Mr|St|Mrs|Ms|Dr)[.]"
        suffixes = "(Inc|Ltd|Jr|Sr|Co)"
        starters = "(Mr|Mrs|Ms|Dr|Prof|Capt|Cpt|Lt|He\s|She\s|It\s|They\s|Their\s|Our\s|We\s|But\s|However\s|That\s|This\s|Wherever)"
        acronyms = "([A-Z][.][A-Z][.](?:[A-Z][.])?)"
        websites = "[.](com|net|org|io|gov|edu|me)"
        digits = "([0-9])"
        multiple_dots = r'\.{2,}'
        text = " " + text + "  "
        text = text.replace("\n"," ")
        text = re.sub(prefixes,"\\1<prd>",text)
        text = re.sub(websites,"<prd>\\1",text)
        text = re.sub(digits + "[.]" + digits,"\\1<prd>\\2",text)
        text = re.sub(multiple_dots, lambda match: "<prd>" * len(match.group(0)) + "<stop>", text)
        if "Ph.D" in text: text = text.replace("Ph.D.","Ph<prd>D<prd>")
        text = re.sub("\s" + alphabets + "[.] "," \\1<prd> ",text)
        text = re.sub(acronyms+" "+starters,"\\1<stop> \\2",text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]" + alphabets + "[.]","\\1<prd>\\2<prd>\\3<prd>",text)
        text = re.sub(alphabets + "[.]" + alphabets + "[.]","\\1<prd>\\2<prd>",text)
        text = re.sub(" "+suffixes+"[.] "+starters," \\1<stop> \\2",text)
        text = re.sub(" "+suffixes+"[.]"," \\1<prd>",text)
        text = re.sub(" " + alphabets + "[.]"," \\1<prd>",text)
        if "”" in text: text = text.replace(".”","”.")
        if "\"" in text: text = text.replace(".\"","\".")
        if "!" in text: text = text.replace("!\"","\"!")
        if "?" in text: text = text.replace("?\"","\"?")
        text = text.replace(".",".<stop>")
        text = text.replace("?","?<stop>")
        text = text.replace("!","!<stop>")
        text = text.replace("<prd>",".")
        sentences = text.split("<stop>")
        sentences = [s.strip() for s in sentences]
        if sentences and not sentences[-1]: sentences = sentences[:-1]
        return sentences
    
    def get_sentence_content(self, position, context_window):
        text = self.sentences[position]
        pre_context, post_context = "", ""
        if position > 0 and context_window > 0:
                pre_context = " ".join(self.sentences[max(0, position-context_window):position]).strip()
        if position < len(self.sentences) - 1 and context_window > 0:
            post_context = " ".join(self.sentences[position+1:min(len(self.sentences)-1, position+context_window+1)]).strip()
        return pre_context + " " + text + " " + post_context
    
class Sentence(Page):
    def __init__(self, sentence, context):
        self.sentence = sentence
        self.context = context
        self.vector = self.embedding(self.sentence)

    def similarity_to_query(self, query_vector):
        """return a scalar on [0,1] to indicate similarity

        Args:
            query (vector): embedding of search query

        Returns:
            _type_: _description_
        """
        return cosine(self.vector, query_vector)
    
    def get_relation_to_query(self, query):
        textual_relations = ["contradiction", "entailment", "neutral"]
        relation = Researcher.classifier(self.sentence + " " + query, textual_relations)
        self.relation_to_query = relation

if __name__ == "__main__":
    pass