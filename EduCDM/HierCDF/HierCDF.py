import sys
import os

import gc
import networkx as nx
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score, mean_squared_error
import time
import torch
import torch.nn as nn
import warnings

from config import hparams, DATA_PATH
from dataloader import TrainDataLoader
from itf import mirt2pl, sigmoid_dot, dot, itf_dict
from tools import Logger, df_preview, labelize, to_numpy

from EduCDM import CDM, re_index

warnings.filterwarnings('ignore')
torch.set_default_tensor_type(torch.DoubleTensor)

class Net(nn.Module):
    '''
    The hierarchical cognitive diagnosis model
    '''
    def __init__(self, n_user, n_item, n_know, hidden_dim, \
        know_graph: pd.DataFrame, itf_type = 'mirt', log_path = './log/'):

        super(Net, self).__init__()
        self.logger = Logger(path = log_path)

        self.n_user = n_user
        self.n_item = n_item
        self.n_know = n_know
        self.hidden_dim = hidden_dim
        self.know_graph = know_graph
        self.know_edge = nx.DiGraph()#nx.DiGraph(know_graph.values.tolist())
        for k in range(n_know):
            self.know_edge.add_node(k)
        for edge in know_graph.values.tolist():
            self.know_edge.add_edge(edge[0],edge[1])

        self.topo_order = list(nx.topological_sort(self.know_edge))

        # the conditional mastery degree when parent is mastered
        condi_p = torch.Tensor(n_user, know_graph.shape[0])
        self.condi_p = nn.Parameter(condi_p)

        # the conditional mastery degree when parent is non-mastered
        condi_n = torch.Tensor(n_user, know_graph.shape[0])
        self.condi_n = nn.Parameter(condi_n)

        # the priori mastery degree of parent
        priori = torch.Tensor(n_user, n_know)
        self.priori = nn.Parameter(priori)

        # item representation
        self.item_diff = nn.Embedding(n_item, n_know)
        self.item_disc = nn.Embedding(n_item, 1)

        # embedding transformation
        self.user_contract = nn.Linear(n_know, hidden_dim)
        self.item_contract = nn.Linear(n_know, hidden_dim)

        # Neural Interaction Module (used only in ncd)
        self.cross_layer1=nn.Linear(hidden_dim,max(int(hidden_dim/2),1))
        self.cross_layer2=nn.Linear(max(int(hidden_dim/2),1),1)

        # layer for featrue cross module
        self.set_itf(itf_type)

        # param initialization
        nn.init.xavier_normal_(self.priori)
        nn.init.xavier_normal_(self.condi_p)
        nn.init.xavier_normal_(self.condi_n)
        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)
    
    def ncd(self, user_emb: torch.Tensor, item_emb: torch.Tensor, item_offset: torch.Tensor):
        input_vec = (user_emb-item_emb)*item_offset
        x_vec=torch.sigmoid(self.cross_layer1(input_vec))
        x_vec=torch.sigmoid(self.cross_layer2(x_vec))
        return x_vec
    
    def set_itf(self, itf_type):
        self.itf_type = itf_type
        self.itf = itf_dict.get(itf_type, self.ncd)

    def get_posterior(self, user_ids: torch.LongTensor, device = 'cpu')->torch.Tensor:
        n_batch = user_ids.shape[0]
        posterior = torch.rand(n_batch, self.n_know).to(device)
        batch_priori = torch.sigmoid(self.priori[user_ids,:])
        batch_condi_p = torch.sigmoid(self.condi_p[user_ids,:])
        batch_condi_n = torch.sigmoid(self.condi_n[user_ids,:])

        # self.logger.write('batch_priori:{}'.format(batch_priori.requires_grad),'console')
        
        #for k in range(self.n_know):
        for k in self.topo_order:
            # get predecessor list
            predecessors = list(self.know_edge.predecessors(k))
            predecessors.sort()
            len_p = len(predecessors)

            # for each knowledge k, do:
            if len_p == 0:
                priori = batch_priori[:,k]
                posterior[:,k] = priori.reshape(-1)
                continue

            # format of masks
            fmt = '{0:0%db}'%(len_p)
            # number of parent master condition
            n_condi = 2 ** len_p

            # sigmoid to limit priori to (0,1)
            #priori = batch_priori[:,predecessors]
            priori = posterior[:,predecessors]

            # self.logger.write('priori:{}'.format(priori.requires_grad),'console')

            pred_idx = self.know_graph[self.know_graph['to'] == k].sort_values(by='from').index
            condi_p = torch.pow(batch_condi_p[:,pred_idx],1/len_p)
            condi_n = torch.pow(batch_condi_n[:,pred_idx],1/len_p)
            
            margin_p = condi_p * priori
            margin_n = condi_n * (1.0-priori)

            posterior_k = torch.zeros((1,n_batch)).to(device)

            for idx in range(n_condi):
                # for each parent mastery condition, do:
                mask = fmt.format(idx)
                mask = torch.Tensor(np.array(list(mask)).astype(int)).to(device)

                margin = mask * margin_p + (1-mask) * margin_n
                margin = torch.prod(margin, dim = 1).unsqueeze(dim = 0)

                posterior_k = torch.cat([posterior_k, margin], dim = 0)
            posterior_k = (torch.sum(posterior_k, dim = 0)).squeeze()
            
            posterior[:,k] = posterior_k.reshape(-1)

        return posterior
    
    def get_condi_p(self,user_ids: torch.LongTensor, device = 'cpu')->torch.Tensor:
        n_batch = user_ids.shape[0]
        result_tensor = torch.rand(n_batch, self.n_know).to(device)
        batch_priori = torch.sigmoid(self.priori[user_ids,:])
        batch_condi_p = torch.sigmoid(self.condi_p[user_ids,:])
        
        #for k in range(self.n_know):
        for k in self.topo_order:
            # get predecessor list
            predecessors = list(self.know_edge.predecessors(k))
            predecessors.sort()
            len_p = len(predecessors)
            if len_p == 0:
                priori = batch_priori[:,k]
                result_tensor[:,k] = priori.reshape(-1)
                continue
            pred_idx = self.know_graph[self.know_graph['to'] == k].sort_values(by='from').index
            condi_p = torch.pow(batch_condi_p[:,pred_idx],1/len_p)
            result_tensor[:,k] = torch.prod(condi_p, dim=1).reshape(-1)
        
        return result_tensor

    def get_condi_n(self,user_ids: torch.LongTensor, device = 'cpu')->torch.Tensor:
        n_batch = user_ids.shape[0]
        result_tensor = torch.rand(n_batch, self.n_know).to(device)
        batch_priori = torch.sigmoid(self.priori[user_ids,:])
        batch_condi_n = torch.sigmoid(self.condi_n[user_ids,:])
        
        #for k in range(self.n_know):
        for k in self.topo_order:
            # get predecessor list
            predecessors = list(self.know_edge.predecessors(k))
            predecessors.sort()
            len_p = len(predecessors)
            if len_p == 0:
                priori = batch_priori[:,k]
                result_tensor[:,k] = priori.reshape(-1)
                continue
            pred_idx = self.know_graph[self.know_graph['to'] == k].sort_values(by='from').index
            condi_n = torch.pow(batch_condi_n[:,pred_idx],1/len_p)
            result_tensor[:,k] = torch.prod(condi_n, dim=1).reshape(-1)
        
        return result_tensor
    
    def concat(self, a, b , dim = 0):
        if a is None:
            return b.reshape(-1,1)
        else:
            return torch.cat([a,b], dim = dim)

    def forward(self, user_ids: torch.LongTensor, item_ids: torch.LongTensor, item_know: torch.Tensor, device = 'cpu')->torch.Tensor:
        '''
        @Param item_know: the item q matrix of the batch
        '''
        user_mastery = self.get_posterior(user_ids,device)
        item_diff = torch.sigmoid(self.item_diff(item_ids))
        item_disc = torch.sigmoid(self.item_disc(item_ids))

        user_factor = torch.tanh(self.user_contract(user_mastery * item_know))
        item_factor = torch.sigmoid(self.item_contract(item_diff * item_know))
        
        output = self.itf(user_factor, item_factor, item_disc)

        return output 

    def train(self, hparams: dict, train_data: pd.DataFrame, Q_matrix: np.array, valid_data: pd.DataFrame = None):
        lr = hparams.get('lr', 0.01)
        epoch = hparams.get('epoch',5)
        batch_size = hparams.get('batch_size', 64)
        logger_mode = hparams.get('logger_mode','both')
        loss_factor = hparams.get('loss_factor',1.0)
        device = hparams.get('device','cpu')
        batch_show = hparams.get('batch_show',200)

        self.logger.write('Before train. hparams = {}'.format(str(hparams)), logger_mode)

        self._to_device(device)

        loss_fn = HierCDLoss(self, nn.NLLLoss, loss_factor)

        dataloader = TrainDataLoader(train_data, Q_matrix, batch_size)

        optimizer = torch.optim.Adam(params = self.parameters(), lr = lr)

        y_target_all = np.array(train_data.loc[:,'score']).astype(np.int)


        for step in range(1, epoch + 1):
            dataloader.reset()
            loss_all = 0
            n_batch = 0
            y_pred_all = np.array([])
            loss_all = 0.0
            batch_count = 0
            while not dataloader.is_end():
                batch_count += 1
                optimizer.zero_grad()
                user_ids, item_ids, item_know, y_target = dataloader.next_batch()
                user_ids = user_ids.to(device)
                item_ids = item_ids.to(device)
                item_know = item_know.to(device)
                y_target = y_target.to(device)
                y_pred = self.forward(user_ids, item_ids, item_know, device)

                output_1 = y_pred
                output_0 = torch.ones(output_1.size()).to(device) - output_1
                output = torch.cat((output_0, output_1), 1)
                loss = loss_fn(torch.log(output), y_target, user_ids)
                loss.backward()
                optimizer.step()

                self.pos_clipper([self.user_contract,self.item_contract])
                self.pos_clipper([self.cross_layer1,self.cross_layer2])

                y_pred_batch = labelize(y_pred)
                y_pred_all = np.concatenate([y_pred_all, y_pred_batch], axis = 0)

                loss_all += loss.item()
                n_batch += 1
                
                if batch_count % batch_show == batch_show - 1:
                    self.logger.write('epoch = {}, batch = {}, loss = {}'.format(
                    step, batch_count, loss_all/batch_show), logger_mode)
                    loss_all = 0.0

            #train_acc = accuracy_score(y_target_all, y_pred_all)
            train_f1 = f1_score(y_target_all, y_pred_all)

            self.logger.write('epoch = {}, train_f1 = {}'.format(step, train_f1),logger_mode)
            if not valid_data is None:
                self.validate(valid_data, Q_matrix, device, logger_mode)
            
    '''
    clip the parameters of each module in the moduleList to nonnegative
    '''
    def pos_clipper(self, module_list: list):
        for module in module_list:
            module.weight.data = module.weight.clamp_min(0)
        return
    
    def neg_clipper(self, module_list: list):
        for module in module_list:
            module.weight.data = module.weight.clamp_max(0)
        return

    def predict(self, data: pd.DataFrame, Q_matrix: np.array, device='cpu')->pd.DataFrame:
        dataloader = TrainDataLoader(data, Q_matrix, 8192)
        dataloader.reset()
        df_pred = pd.DataFrame(columns=['predict_score','predict_label'])
        self._to_device(device)
        while not dataloader.is_end():
            user_ids, item_ids, item_know, _ = dataloader.next_batch()
            user_ids = user_ids.to(device)
            item_ids = item_ids.to(device)
            item_know = item_know.to(device)

            z_output = self.forward(user_ids, item_ids, item_know, device = device)
            z_score = to_numpy(z_output).reshape(-1)
            z_label = labelize(z_output).reshape(-1)
            df_batch = pd.DataFrame({
                'predict_score': z_score,
                'predict_label': z_label
            })
            df_pred = df_pred.append(df_batch,ignore_index=True)
        
        result = data.reset_index().join(df_pred)

        return result

    def validate(self, valid_data: pd.DataFrame, Q_matrix: np.array, device, logger_mode):
        valid_pred = self.predict(valid_data, Q_matrix, device)
        z_true = valid_pred['score'].astype(int).tolist()
        z_score = valid_pred['predict_score'].tolist()
        z_label = valid_pred['predict_label'].tolist()

        valid_acc = accuracy_score(z_true, z_label)
        valid_auc = roc_auc_score(z_true, z_score)
        valid_f1 = f1_score(z_true, z_label)
        valid_mse = mean_squared_error(z_true, z_score)

        self.logger.write('valid acc = {}'.format(valid_acc),logger_mode)
        self.logger.write('valid f1  = {}'.format(valid_f1),logger_mode)
        self.logger.write('valid auc = {}'.format(valid_auc),logger_mode)
        self.logger.write('valid mse = {}\n'.format(valid_mse),logger_mode)

        metrics_dict = {}
        metrics_dict['acc']=(valid_acc)
        metrics_dict['f1']=(valid_f1)
        metrics_dict['auc']=(valid_auc)
        metrics_dict['mse']=(valid_mse)

        return metrics_dict

    def _to_device(self, device):
        self.priori = nn.Parameter(self.priori.to(device))
        self.condi_p = nn.Parameter(self.condi_p.to(device))
        self.condi_n = nn.Parameter(self.condi_n.to(device))
        self.user_contract = self.user_contract.to(device)
        self.item_contract = self.item_contract.to(device)
        self.item_diff = self.item_diff.to(device)
        self.item_disc = self.item_disc.to(device)
        self.cross_layer1=self.cross_layer1.to(device)
        self.cross_layer2=self.cross_layer2.to(device)
    
    def save(self, model_name = './model.pkl'):
        path = '/'.join(model_name.split('/')[:-1])+'/'
        if not os.path.exists(path):
            os.makedirs(path)
        torch.save(self.state_dict(), model_name)

    def load(self, model_name = './model.pkl'):
        self.load_state_dict(torch.load(model_name))

class HierCDF(CDM):
    r'''
    The HierCDF model.
    Args:
        meta_data: a dictionary containing all the userIds, itemIds, and skills.
        knowledge_graph: pd.DataFrame, columns = ['source','target'], a data frame containing all directed edge of the knowledge graph (attribute hierarchy). The `source` of each row denotes the name of the source vertex, while the `target` of each row denotes the name of the target vertex.

    Examples::
        meta_data = {'userId': ['001', '002', '003'], 'itemId': ['adf', 'w5'], 'skill': ['skill1', 'skill2', 'skill3', 'skill4']}
        model = HierCDF(meta_data, know_graph)
    '''
    def __init__(self, meta_data: dict, knowledge_graph:pd.DataFrame, hidd_dim:int):
        super(HierCDF,self).__init__()
        self.id_reindex, _ = re_index(meta_data)
        self.student_n = len(self.id_reindex['userId'])
        self.exer_n = len(self.id_reindex['itemId'])
        self.knowledge_n = len(self.id_reindex['skill'])
        trans_know_graph = {'source':[],'target':[]}
        for id, row in knowledge_graph.iterrows():
            trans_know_graph['source'].append(\
                self.id_reindex['skill'][row['source']])
            trans_know_graph['target'].append(\
                self.id_reindex['skill'][row['target']])
        trans_know_graph = pd.DataFrame(trans_know_graph)
        self.hier_net = Net(self.student_n, self.exer_n, \
            self.knowledge_n, hidd_dim, itf_type = 'mirt')
    
    def transform__(self, df_data: pd.DataFrame, batch_size: int, shuffle):
        users = [self.id_reindex['userId'][userId] for userId in df_data['userId'].values]
        items = [self.id_reindex['itemId'][itemId] for itemId in df_data['itemId'].values]
        responses = df_data['response'].values
        knowledge_emb = torch.zeros((len(df_data), self.knowledge_n))
        for idx, skills in enumerate(df_data['skill']):
            skills = eval(skills)  # str of list to list
            for skill in skills:
                skill_reindex = self.id_reindex['skill'][skill]
                knowledge_emb[idx][skill_reindex] = 1.0

        data_set = TensorDataset(
            torch.tensor(users, dtype=torch.int64),
            torch.tensor(items, dtype=torch.int64),
            knowledge_emb,
            torch.tensor(responses, dtype=torch.float32)
        )
        return DataLoader(data_set, batch_size=batch_size, shuffle=shuffle)
    
    def fit(self, train_data: pd.DataFrame, epoch: int, val_data=None, \
        device="cpu", lr=0.002, batch_size=64, loss_factor=1.0):
        self.hier_net = self.hier_net.to(device)
        self.hier_net.train()
        loss_function = HierCDLoss(self.hier_net, nn.NLLLoss, loss_factor)
        optimizer = torch.optim.Adam(params = self.hiernet.parameters(), lr = lr)
        for epoch_i in range(epoch):
            self.hier_net.train()
            epoch_losses = []
            batch_count = 0
            for batch_data in tqdm(train_data, "Epoch %s" % epoch_i):
                batch_count += 1
                user_id, item_id, knowledge_emb, y_true = batch_data
                user_id: torch.Tensor = user_id.to(device)
                item_id: torch.Tensor = item_id.to(device)
                knowledge_emb: torch.Tensor = knowledge_emb.to(device)
                y_true: torch.Tensor = y_true.to(device)
                y_pred: torch.Tensor = self.hier_net.forward(\
                    user_id,item_id,knowledge_emb,device)
                y_pred_neg = torch.ones(y_pred.size()).to(device) - y_pred
                output = torch.cat((y_pred_neg, y_pred),1)
                loss = loss_function(torch.log(output), y_true, user_ids)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                epoch_losses.append(loss.mean().item())

            print("[Epoch %d] average loss: %.6f" % (epoch_i, float(np.mean(epoch_losses))))

            # TODO eval(val_data)
    
    def predict_proba(self, test_data: pd.DataFrame, device="cpu") -> pd.DataFrame:
        r'''
        Output the predicted probabilities that the users would provide correct answers using test_data.
        The probabilities are within (0, 1).

        Args:
            test_data: a dataframe containing testing userIds and itemIds.
            device: device on which the model is trained. Default: 'cpu'. If you want to run it on your
                    GPU, e.g., the first cuda gpu on your machine, you can change it to 'cuda:0'.

        Return:
            a dataframe containing the userIds, itemIds, and proba (predicted probabilities).
        '''

        self.hier_net = self.hier_net.to(device)
        self.hier_net.eval()
        test_loader = self.transform__(test_data, batch_size=64, shuffle=False)
        pred_proba = []
        with torch.no_grad():
            for batch_data in tqdm(test_loader, "Predicting"):
                user_id, item_id, knowledge_emb, y = batch_data
                user_id: torch.Tensor = user_id.to(device)
                item_id: torch.Tensor = item_id.to(device)
                knowledge_emb: torch.Tensor = knowledge_emb.to(device)
                pred: torch.Tensor = self.hier_net(user_id, item_id, \
                    knowledge_emb, device)
                pred_proba.extend(pred.detach().cpu().tolist())
        ret = pd.DataFrame({'userId': test_data['userId'], 'itemId': test_data['itemId'], 'proba': pred_proba})
        return ret
    
    def predict(self, test_data: pd.DataFrame, device="cpu") -> pd.DataFrame:
        r'''
        Output the predicted responses using test_data. The responses are either 0 or 1.

        Args:
            test_data: a dataframe containing testing userIds and itemIds.
            device: device on which the model is trained. Default: 'cpu'. If you want to run it on your
                    GPU, e.g., the first cuda gpu on your machine, you can change it to 'cuda:0'.

        Return:
            a dataframe containing the userIds, itemIds, and predicted responses.
        '''

        df_proba = self.predict_proba(test_data, device)
        y_pred = [1.0 if proba >= 0.5 else 0 for proba in df_proba['proba'].values]
        df_pred = pd.DataFrame({'userId': df_proba['userId'], 'itemId': df_proba['itemId'], 'proba': y_pred})

        return df_pred
    
    def eval(self, val_data: pd.DataFrame, device="cpu") -> Tuple[float, float]:
        r'''
        Output the AUC and accuracy using the val_data.

        Args:
            val_data: a dataframe containing testing userIds and itemIds.
            device: device on which the model is trained. Default: 'cpu'. If you want to run it on your
                    GPU, e.g., the first cuda gpu on your machine, you can change it to 'cuda:0'.

        Return:
            AUC, accuracy
        '''

        y_true = val_data['response'].values
        df_proba = self.predict_proba(val_data, device)
        pred_proba = df_proba['proba'].values
        return roc_auc_score(y_true, pred_proba), \
            accuracy_score(y_true, np.array(pred_proba) >= 0.5)
    
    def save(self, filepath: str):
        r'''
        Save the model. This method is implemented based on the PyTorch's torch.save() method. Only the parameters
        in self.ncdm_net will be saved. You can save the whole NCDM object using pickle.

        Args:
            filepath: the path to save the model.
        '''

        torch.save(self.hier_net.state_dict(), filepath)
        logging.info("save parameters to %s" % filepath)

    def load(self, filepath: str):
        r'''
        Load the model. This method loads the model saved at filepath into self.ncdm_net. Before loading, the object
        needs to be properly initialized.

        Args:
            filepath: the path from which to load the model.

        Examples:
            model = HierCDF(meta_data, knowledge_graph)  
                # where meta_data is from the same dataset
                # which is used to train the model at filepath
            model.load('path_to_the_pre-trained_model')
        '''

        self.hier_net.load_state_dict(torch.load(filepath, map_location=lambda s, loc: s))
        logging.info("load parameters from %s" % filepath)

class HierCDLoss(nn.Module):
    '''
    The loss function of HierCDM
    '''
    def __init__(self, net: Net, loss_fn: nn.Module, factor = 1.0):
        super(HierCDLoss, self).__init__()
        self.net = net
        self.factor = factor
        self.loss_fn = loss_fn()
    def forward(self, y_pred, y_target, user_ids):
        return self.loss_fn(y_pred, y_target) + self.factor * torch.sum(torch.relu(self.net.condi_n[user_ids,:]-self.net.condi_p[user_ids,:]))

def test(hparams):
    n_user = hparams['n_user']
    n_item = hparams['n_item']
    n_know = hparams['n_know']
    hidden_dim = hparams['hidden_dim']

    data = pd.read_csv(DATA_PATH+'data_demo.csv',index_col = 0)
    data = data.sample(frac=1).reset_index(drop=True)
    print(data.head())
    know_graph = pd.read_csv(DATA_PATH+'hierarchy_demo.csv',index_col = 0)
    Q_matrix = np.loadtxt(DATA_PATH+'Q_matrix_demo.txt', delimiter=' ')

    net = HierCDM(n_user, n_item, n_know, hidden_dim, know_graph)

    net.train(hparams = hparams, Q_matrix=Q_matrix, train_data = data)

if __name__ == '__main__':
    test(hparams)
